from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from tixati_qbittorrent.tixati_core import _rc4
from tixati_qbittorrent.tixati_settings import (
    SETTINGS_KEY,
    SettingsFormatError,
    read_settings,
)


def _entry(key: str | int, value_type: int, payload: bytes) -> bytes:
    if isinstance(key, int):
        encoded_key = struct.pack("<I", 5) + b"\x04" + struct.pack("<I", key)
    else:
        encoded_key_bytes = key.encode("utf-8")
        encoded_key = (
            struct.pack("<I", len(encoded_key_bytes) + 1)
            + b"\x02"
            + encoded_key_bytes
        )
    encoded_value = struct.pack("<I", len(payload) + 1) + bytes([value_type]) + payload
    return encoded_key + encoded_value


def _path(*components: str) -> bytes:
    payload = [struct.pack("<I", len(components))]
    for component in components:
        encoded = component.encode("utf-8")
        payload.extend((struct.pack("<I", len(encoded) + 1), b"\x02", encoded))
    return b"".join(payload)


def _settings_file(path: Path) -> None:
    entries = b"".join(
        (
            _entry("incport", 3, struct.pack("<I", 15363)),
            _entry("incpath", 12, _path("home", "kasumi", "Downloads")),
            _entry(0x12345678, 3, struct.pack("<I", 1)),
        )
    )
    payload = struct.pack("<I", 3) + entries
    plaintext = struct.pack("<IB", len(payload) + 1, 11) + payload
    raw = b"\x02\x00\x00\x00" + _rc4(plaintext, SETTINGS_KEY)
    path.write_bytes(raw + hashlib.sha1(plaintext).digest())


def test_settings_reader_decodes_named_and_numeric_entries(tmp_path: Path) -> None:
    source = tmp_path / "settings2.dat"
    _settings_file(source)

    settings = read_settings(source)

    assert len(settings.entries) == 3
    assert settings.get("incport") == 15363
    assert settings.path("incpath") == "/home/kasumi/Downloads"
    numeric = settings.entries[2]
    assert numeric.field_id == 0x12345678
    assert numeric.value == 1


def test_settings_reader_rejects_bad_integrity(tmp_path: Path) -> None:
    source = tmp_path / "settings2.dat"
    _settings_file(source)
    data = bytearray(source.read_bytes())
    data[-1] ^= 0x01
    source.write_bytes(data)

    with pytest.raises(SettingsFormatError, match="integrity"):
        read_settings(source)
