"""Reverse-engineered reader for Tixati v2 ``core2.dat`` files.

The format is not a standard archive.  Version-2 core files contain a custom
field stream encrypted with an RC4-compatible stream and authenticated by a
SHA-1 trailer.  The transfer records store the bencoded ``info`` dictionary in
three pieces: a prefix, the piece-hash bytes, and a suffix.  This module only
uses the fields needed to rebuild torrent metadata; unknown fields are safely
ignored.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .bencode import BValue, BencodeError, decode, encode


# This is the 24-byte key used by the v2 core reader.  It is independent of
# the user's profile contents; the profile data remains encrypted on disk.
CORE_KEY = bytes.fromhex("2b6fa3709a12130aad251db6d442d38819155b9e3270f9cc")
CORE_VERSION = 2
# The five-byte preamble is stable, but the body also contains nested opaque
# data. We therefore scan for this marker instead of assuming that every byte
# belongs to a top-level record.
_FIELD_MARKER = b"\x05\x00\x00\x00\x04"
_PIECES_RE = re.compile(rb"pieces([0-9]+):$")
# CLI code supplies this callback so long-running scans can report progress;
# the library remains silent when it is omitted.
Progress = Callable[[str], None]


class CoreFormatError(ValueError):
    """Raised when a Tixati core file cannot be authenticated or decoded."""


@dataclass(frozen=True)
class CoreField:
    """One recognized field in the decrypted Tixati field stream."""

    position: int  # Byte offset in the decrypted body; used for record regions.
    field_id: int  # Tixati's private numeric field identifier.
    value_type: int  # The serialized type byte; 2 is the byte-string type here.
    payload: bytes  # Length-prefixed value with the type byte removed.


@dataclass(frozen=True)
class CoreTransfer:
    """Torrent metadata reconstructed from one Tixati transfer record."""

    info_hash: bytes  # SHA-1 of the exact bencoded info_bytes.
    info: dict[bytes, BValue]  # Decoded form used for validation and fastresume.
    info_bytes: bytes  # Keep raw bytes: re-encoding can change a noncanonical hash.
    torrent_path: str  # Diagnostic source path or Tixati hash-link label.
    trackers: tuple[bytes, ...]  # URLs found in this transfer's field region.

    @property
    def torrent_bytes(self) -> bytes:
        """Return a valid root torrent dictionary containing this info dict."""

        # Keep the exact info bytes.  Tixati normally emits canonical bencode,
        # but records can preserve a different dictionary-key order.
        # Re-encoding that dictionary would change its info hash.
        encoded_values: dict[bytes, bytes] = {b"info": self.info_bytes}
        if self.trackers:
            encoded_values[b"announce"] = encode(self.trackers[0])
            encoded_values[b"announce-list"] = encode([list(self.trackers)])
        parts = [b"d"]
        for key in sorted(encoded_values):
            parts.extend((encode(key), encoded_values[key]))
        parts.append(b"e")
        return b"".join(parts)


@dataclass(frozen=True)
class CoreExtraction:
    """Successful transfers plus hash-only records that were not convertible."""

    transfers: tuple[CoreTransfer, ...]
    skipped_info_hashes: tuple[bytes, ...]


def _rc4(data: bytes, key: bytes = CORE_KEY) -> bytes:
    """Apply the RC4-compatible stream used by Tixati's core reader."""

    # PyCryptodome keeps the normal migration path fast for the large core
    # files produced by long-lived profiles.  The fallback below keeps the
    # module usable in a minimal Python environment and is also convenient for
    # small synthetic fixtures.
    try:
        from Crypto.Cipher import ARC4
    except ImportError:  # pragma: no cover - exercised only without optional C code
        ARC4 = None
    if ARC4 is not None:
        return ARC4.new(key).decrypt(data)

    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]

    out = bytearray(len(data))
    i = 0
    j = 0
    for offset, value in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out[offset] = value ^ state[(state[i] + state[j]) & 0xFF]
    return bytes(out)


def decrypt_core(path: str | Path, *, progress: Progress | None = None) -> bytes:
    """Decrypt and authenticate a version-2 ``core2.dat`` file."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise CoreFormatError(f"cannot read {source}: {exc}") from exc
    if len(raw) < 4 + 20:
        raise CoreFormatError(f"{source} is too small to be a core2.dat file")
    if progress is not None:
        progress(f"read {source.name} ({len(raw) / 1024 / 1024:.1f} MiB)")
    version = int.from_bytes(raw[:4], "little")
    if version != CORE_VERSION:
        raise CoreFormatError(
            f"{source} has unsupported core format version {version}; "
            f"expected {CORE_VERSION}"
        )
    if progress is not None:
        progress("decrypting core payload")
    plaintext = _rc4(raw[4:-20])
    if progress is not None:
        progress("checking core SHA-1 integrity")
    expected = raw[-20:]
    actual = hashlib.sha1(plaintext).digest()
    if actual != expected:
        raise CoreFormatError(
            f"{source} failed its SHA-1 integrity check; it may be a newer or "
            "incompatible Tixati format"
        )
    return plaintext


def _scan_fields(plaintext: bytes) -> list[CoreField]:
    """Find the known field records in the custom serialized stream.

    Tixati nests additional opaque structures in the same stream.  Scanning
    for the stable field marker and retaining only IDs used by transfer
    metadata avoids having to guess the meaning of every private field.
    """

    wanted = {0x11003, 0x11009, 0x1101E, 0x11027, 0x11028, 0x12001}
    fields: list[CoreField] = []
    position = 0
    while True:
        position = plaintext.find(_FIELD_MARKER, position)
        if position < 0:
            break
        header_end = position + len(_FIELD_MARKER)
        if header_end + 8 > len(plaintext):
            break
        field_id = int.from_bytes(plaintext[position + 5 : position + 9], "little")
        encoded_length = int.from_bytes(
            plaintext[position + 9 : position + 13], "little"
        )
        value_type = plaintext[position + 13]
        payload_start = position + 14
        payload_end = position + 13 + encoded_length
        if (
            encoded_length >= 1
            and payload_end <= len(plaintext)
            and field_id in wanted
        ):
            fields.append(
                CoreField(
                    position=position,
                    field_id=field_id,
                    value_type=value_type,
                    payload=plaintext[payload_start:payload_end],
                )
            )
        # Do not jump by encoded_length: a valid field may be nested in an
        # opaque structure, and false marker-looking bytes can occur in a
        # payload. The known-ID and bounds checks above filter those safely.
        position += 1
    return fields


def _region(fields: list[CoreField], start: int, end: int, field_id: int) -> list[CoreField]:
    # `_scan_fields` emits fields in byte order. Restricting lookup to the
    # interval between two info-hash records associates paths and trackers with
    # one transfer without interpreting the rest of Tixati's object graph.
    positions = [field.position for field in fields]
    left = bisect.bisect_left(positions, start)
    right = bisect.bisect_left(positions, end)
    return [field for field in fields[left:right] if field.field_id == field_id]


def extract_core(
    plaintext: bytes,
    *,
    progress: Progress | None = None,
) -> CoreExtraction:
    """Reconstruct torrent metadata from decrypted core plaintext."""

    if progress is not None:
        progress("scanning serialized transfer fields")
    fields = _scan_fields(plaintext)
    if progress is not None:
        progress(f"found {len(fields)} relevant fields")
    hash_fields = [
        field
        for field in fields
        if field.field_id == 0x11009
        and field.value_type == 2
        and len(field.payload) == 20
    ]
    # The info-hash table is both the transfer index and the integrity check
    # for pairing a prefix with the correct piece-hash blob.
    hash_by_value = {field.payload: field for field in hash_fields}
    hash_positions = [field.position for field in hash_fields]

    piece_fields = [
        field
        for field in fields
        if field.field_id == 0x11003 and field.value_type == 2
    ]
    pieces_by_length: dict[int, list[CoreField]] = {}
    for field in piece_fields:
        pieces_by_length.setdefault(len(field.payload), []).append(field)
    piece_positions = {
        length: [field.position for field in candidates]
        for length, candidates in pieces_by_length.items()
    }
    prefixes = [
        field
        for field in fields
        if field.field_id == 0x11027 and field.value_type == 2
    ]
    suffixes = [
        field
        for field in fields
        if field.field_id == 0x11028 and field.value_type == 2
    ]
    prefix_positions = [field.position for field in prefixes]

    found: dict[bytes, CoreTransfer] = {}
    for index, prefix in enumerate(prefixes):
        if progress is not None and (index == 0 or (index + 1) % 250 == 0):
            progress(f"reconstructing torrent metadata {index + 1}/{len(prefixes)}")
        match = _PIECES_RE.search(prefix.payload)
        if match is None:
            continue
        # The number in `pieces<N>:` is the byte length of the external field,
        # not the torrent piece size. Several torrents can share that length,
        # so candidates are tested by the resulting SHA-1 rather than position
        # alone.
        piece_length = int(match.group(1))
        candidates = pieces_by_length.get(piece_length, [])
        candidate_positions = piece_positions.get(piece_length, [])
        candidate_index = bisect.bisect_left(candidate_positions, prefix.position) - 1
        next_prefix = (
            prefix_positions[index + 1]
            if index + 1 < len(prefix_positions)
            else len(plaintext)
        )
        # The suffix is the next 0x11028 field before another info prefix. It
        # commonly contains only `e`, but private keys such as `private` may
        # appear there for some torrents.
        suffix_candidates = [
            field
            for field in suffixes
            if prefix.position < field.position < next_prefix
        ]
        if not suffix_candidates:
            continue
        suffix = suffix_candidates[0]

        # Usually the closest preceding piece field is exact.  Trying older
        # same-sized candidates as well makes the parser robust around the
        # hash-link records, which do not carry piece hashes.
        while candidate_index >= 0:
            piece = candidates[candidate_index]
            info_bytes = prefix.payload + piece.payload + suffix.payload
            info_hash = hashlib.sha1(info_bytes).digest()
            if info_hash in hash_by_value:
                try:
                    info_value = decode(info_bytes)
                except BencodeError:
                    candidate_index -= 1
                    continue
                if not isinstance(info_value, dict):
                    candidate_index -= 1
                    continue
                hash_field = hash_by_value[info_hash]
                hash_index = bisect.bisect_left(hash_positions, hash_field.position)
                next_hash = (
                    hash_positions[hash_index + 1]
                    if hash_index + 1 < len(hash_positions)
                    else len(plaintext)
                )
                path_fields = _region(fields, hash_field.position, next_hash, 0x1101E)
                tracker_fields = _region(fields, hash_field.position, next_hash, 0x12001)
                path_value = path_fields[0].payload if path_fields else b""
                try:
                    torrent_path = path_value.decode("utf-8")
                except UnicodeDecodeError:
                    torrent_path = path_value.decode("utf-8", "replace")
                trackers = tuple(
                    field.payload
                    for field in tracker_fields
                    if b"://" in field.payload
                )
                found[info_hash] = CoreTransfer(
                    info_hash=info_hash,
                    info=info_value,
                    info_bytes=info_bytes,
                    torrent_path=torrent_path,
                    trackers=trackers,
                )
                break
            candidate_index -= 1

    transfers = tuple(
        found[hash_field.payload]
        for hash_field in hash_fields
        if hash_field.payload in found
    )
    skipped = tuple(
        hash_field.payload
        for hash_field in hash_fields
        if hash_field.payload not in found
    )
    if progress is not None:
        progress(
            f"reconstructed {len(transfers)} transfers; "
            f"{len(skipped)} records skipped"
        )
    return CoreExtraction(transfers=transfers, skipped_info_hashes=skipped)


def extract_core_file(
    path: str | Path,
    *,
    progress: Progress | None = None,
) -> CoreExtraction:
    """Decrypt, authenticate and extract transfers from ``core2.dat``."""

    return extract_core(decrypt_core(path, progress=progress), progress=progress)
