"""Reader for Tixati v2 ``settings2.dat`` files.

``settings2.dat`` uses the same version/header/SHA-1 envelope and the same
RC4-compatible stream transform as ``core2.dat``, but it has its own stable
24-byte key.  The decrypted body is a counted registry.  Each registry entry
has either a numeric field identifier or a UTF-8 setting name, followed by a
typed value.  Unknown value types are intentionally retained as raw bytes so
that adding a new Tixati setting cannot silently corrupt a migration.

This module stops at decoding the settings database.  Mapping settings to a
different client's configuration belongs in :mod:`config`, where each mapping
can be reviewed independently.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .tixati_core import _rc4


# This is the key observed in the v2 settings loader.  It is separate from
# CORE_KEY: using the core key decrypts a settings body into garbage and fails
# the SHA-1 check.  Keeping the key here, rather than deriving it from a user
# path, matches Tixati's cross-machine configuration behavior.
SETTINGS_KEY = bytes.fromhex(
    "4d90de01dd6edfde4c4dbd299b0ae6c2a8f0a8679c980fff"
)
SETTINGS_VERSION = 2
_OUTER_VALUE_TYPE = 11

Progress = Callable[[str], None]


class SettingsFormatError(ValueError):
    """Raised when a ``settings2.dat`` file is malformed or unauthenticated."""


@dataclass(frozen=True)
class TixatiSetting:
    """One entry in the decrypted settings registry.

    ``name`` is present for the normal string-keyed settings.  A few legacy
    registry entries use a numeric field identifier instead; those entries
    remain available through ``field_id`` and ``payload`` for future work.
    ``value_type`` is Tixati's private serializer type number and ``payload``
    excludes the type byte and its length word.
    """

    name: str | None
    field_id: int | None
    value_type: int
    payload: bytes

    @property
    def value(self) -> object:
        """Return a conservative Python representation of the value.

        String values are decoded as UTF-8 when possible.  Integer-like types
        are decoded little-endian.  Composite and unknown values stay bytes;
        callers can still inspect their exact serialized payload.
        """

        if self.value_type == 2:
            try:
                return self.payload.decode("utf-8")
            except UnicodeDecodeError:
                return self.payload
        if self.value_type in (3, 4, 5) and len(self.payload) == 4:
            return int.from_bytes(self.payload, "little", signed=False)
        if self.value_type == 6 and len(self.payload) == 8:
            return int.from_bytes(self.payload, "little", signed=False)
        return self.payload


@dataclass(frozen=True)
class TixatiSettings:
    """Authenticated, decoded Tixati settings."""

    entries: tuple[TixatiSetting, ...]

    def named(self) -> Iterator[TixatiSetting]:
        """Iterate over ordinary string-keyed settings."""

        return (entry for entry in self.entries if entry.name is not None)

    def get_entry(self, name: str) -> TixatiSetting | None:
        """Return the last entry with ``name``, or ``None`` if absent."""

        result: TixatiSetting | None = None
        for entry in self.entries:
            if entry.name == name:
                result = entry
        return result

    def get(self, name: str, default: object = None) -> object:
        """Return a decoded named setting or ``default``."""

        entry = self.get_entry(name)
        return default if entry is None else entry.value

    def path(self, name: str) -> str | None:
        """Decode a Tixati type-12 path-list setting.

        Tixati stores paths as a list of UTF-8 components, not as a native
        platform path string.  The leading slash is part of the observed
        representation and preserves the absolute-path meaning used by the
        client on Linux.
        """

        entry = self.get_entry(name)
        if entry is None or entry.value_type != 12:
            return None
        return _decode_path(entry.payload)


def decrypt_settings(path: str | Path, *, progress: Progress | None = None) -> bytes:
    """Decrypt and authenticate a version-2 ``settings2.dat`` file."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SettingsFormatError(f"cannot read {source}: {exc}") from exc
    if len(raw) < 4 + 20:
        raise SettingsFormatError(f"{source} is too small to be a settings2.dat file")
    version = int.from_bytes(raw[:4], "little")
    if version != SETTINGS_VERSION:
        raise SettingsFormatError(
            f"{source} has unsupported settings format version {version}; "
            f"expected {SETTINGS_VERSION}"
        )
    if progress is not None:
        progress(f"read {source.name} ({len(raw) / 1024 / 1024:.1f} MiB)")
        progress("decrypting settings payload")
    plaintext = _rc4(raw[4:-20], SETTINGS_KEY)
    if progress is not None:
        progress("checking settings SHA-1 integrity")
    expected = raw[-20:]
    actual = hashlib.sha1(plaintext).digest()
    if actual != expected:
        raise SettingsFormatError(
            f"{source} failed its SHA-1 integrity check; it may use a newer or "
            "incompatible Tixati format"
        )
    return plaintext


def parse_settings(plaintext: bytes) -> TixatiSettings:
    """Parse the counted registry body returned by :func:`decrypt_settings`."""

    if len(plaintext) < 9:
        raise SettingsFormatError("settings payload is too short")
    outer_length, outer_type = struct.unpack_from("<IB", plaintext, 0)
    if outer_type != _OUTER_VALUE_TYPE:
        raise SettingsFormatError(
            f"unexpected settings root type {outer_type}; expected {_OUTER_VALUE_TYPE}"
        )
    if outer_length != len(plaintext) - 4:
        raise SettingsFormatError(
            "settings root length does not match the decrypted payload size"
        )

    entry_count = _u32(plaintext, 5, "settings entry count")
    position = 9
    entries: list[TixatiSetting] = []
    for index in range(entry_count):
        key_length = _u32(plaintext, position, f"entry {index} key length")
        key_type = _byte(plaintext, position + 4, f"entry {index} key type")
        key_start = position + 5
        key_end = position + 4 + key_length
        if key_length < 1 or key_end > len(plaintext):
            raise SettingsFormatError(f"entry {index} has an invalid key length")
        if key_type == 4 and key_length == 5:
            field_id = _u32(plaintext, key_start, f"entry {index} field id")
            name = None
        elif key_type == 2:
            try:
                name = plaintext[key_start:key_end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SettingsFormatError(f"entry {index} has an invalid UTF-8 name") from exc
            field_id = None
        else:
            raise SettingsFormatError(
                f"entry {index} has unsupported key type {key_type}"
            )

        value_length = _u32(plaintext, key_end, f"entry {index} value length")
        value_type = _byte(plaintext, key_end + 4, f"entry {index} value type")
        value_start = key_end + 5
        value_end = key_end + 4 + value_length
        if value_length < 1 or value_end > len(plaintext):
            raise SettingsFormatError(f"entry {index} has an invalid value length")
        entries.append(
            TixatiSetting(
                name=name,
                field_id=field_id,
                value_type=value_type,
                payload=plaintext[value_start:value_end],
            )
        )
        position = value_end

    if position != len(plaintext):
        raise SettingsFormatError(
            f"settings registry has {len(plaintext) - position} trailing bytes"
        )
    return TixatiSettings(tuple(entries))


def read_settings(path: str | Path, *, progress: Progress | None = None) -> TixatiSettings:
    """Read, authenticate and parse a Tixati settings file."""

    return parse_settings(decrypt_settings(path, progress=progress))


def _decode_path(payload: bytes) -> str | None:
    if len(payload) < 4:
        return None
    component_count = int.from_bytes(payload[:4], "little")
    position = 4
    components: list[str] = []
    for _ in range(component_count):
        if position + 5 > len(payload):
            return None
        length = int.from_bytes(payload[position : position + 4], "little")
        value_type = payload[position + 4]
        if length < 1 or value_type != 2:
            return None
        value_end = position + 4 + length
        if value_end > len(payload):
            return None
        try:
            components.append(payload[position + 5 : value_end].decode("utf-8"))
        except UnicodeDecodeError:
            return None
        position = value_end
    if not components or position != len(payload):
        return None
    return "/" + "/".join(components)


def _u32(data: bytes, position: int, label: str) -> int:
    if position + 4 > len(data):
        raise SettingsFormatError(f"truncated {label}")
    return struct.unpack_from("<I", data, position)[0]


def _byte(data: bytes, position: int, label: str) -> int:
    if position >= len(data):
        raise SettingsFormatError(f"truncated {label}")
    return data[position]
