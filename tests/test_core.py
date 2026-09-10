from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from tixati_qbittorrent.bencode import decode
from tixati_qbittorrent.migration import migrate
from tixati_qbittorrent.tixati_core import (
    _rc4,
    decrypt_core,
    extract_core_file,
)


def _field(field_id: int, value_type: int, payload: bytes) -> bytes:
    return (
        b"\x05\x00\x00\x00\x04"
        + struct.pack("<I", field_id)
        + struct.pack("<I", len(payload) + 1)
        + bytes([value_type])
        + payload
    )


def _generic_string(key: bytes, value: bytes) -> bytes:
    """Encode one entry from Tixati's transfer settings map."""

    return (
        struct.pack("<I", len(key) + 1)
        + b"\x02"
        + key
        + struct.pack("<I", len(value) + 1)
        + b"\x02"
        + value
    )


def _fixture_core() -> bytes:
    pieces = bytes(range(40))
    # Deliberately keep a non-canonical key order.  The real profile contains
    # one record like this, and changing the order would change its info hash.
    prefix = b"d4:name11:fixture.bin6:lengthi123e6:pieces40:"
    suffix = b"12:piece lengthi32ee"
    info_bytes = prefix + pieces + suffix
    info_hash = hashlib.sha1(info_bytes).digest()

    plaintext = b"".join(
        (
            _field(0x11003, 2, pieces),
            _field(0x11009, 2, info_hash),
            _field(0x11006, 6, struct.pack("<Q", 1_590_000_000)),
            _field(0x11007, 2, b"fixture comment"),
            _field(0x11008, 2, b"fixture creator"),
            _field(0x1103F, 6, struct.pack("<Q", 1_600_000_123)),
            _field(0x1101E, 2, b"/old/fixture.torrent"),
            _field(0x11027, 2, prefix),
            _field(0x11028, 2, suffix),
            _field(0x12001, 2, b"https://tracker.example/announce"),
            _generic_string(b"d_category", b"fixture-category"),
            # A hash-only record, analogous to Tixati's hash-link entries.
            _field(0x11009, 2, b"x" * 20),
            _field(0x1101E, 2, b"hash-link: x"),
        )
    )
    return (
        struct.pack("<I", 2)
        + _rc4(plaintext)
        + hashlib.sha1(plaintext).digest()
    )


def test_core_decrypts_and_reconstructs_torrent(tmp_path: Path) -> None:
    core = tmp_path / "core2.dat"
    core.write_bytes(_fixture_core())

    assert decrypt_core(core)[:5] == b"\x05\x00\x00\x00\x04"
    extraction = extract_core_file(core)

    assert len(extraction.transfers) == 1
    assert extraction.skipped_info_hashes == (b"x" * 20,)
    transfer = extraction.transfers[0]
    assert transfer.info_hash == hashlib.sha1(transfer.info_bytes).digest()
    assert transfer.created_time == 1_600_000_123
    assert transfer.creation_date == 1_590_000_000
    assert transfer.comment == b"fixture comment"
    assert transfer.created_by == b"fixture creator"
    assert transfer.category == "fixture-category"
    root = decode(transfer.torrent_bytes)
    assert isinstance(root, dict)
    assert root[b"info"] == transfer.info
    assert root[b"creation date"] == 1_590_000_000
    assert root[b"comment"] == b"fixture comment"
    assert root[b"created by"] == b"fixture creator"
    assert root[b"announce"] == b"https://tracker.example/announce"
    assert transfer.torrent_bytes.endswith(transfer.info_bytes + b"e")


def test_migrate_uses_core_when_no_torrent_source_is_given(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    (tixati / "core2.dat").write_bytes(_fixture_core())
    destination = tmp_path / "BT_backup"

    report = migrate(tixati, destination, now=1_700_000_000)

    assert report.torrent_count == 1
    assert report.skipped_count == 1
    stem = next(path.stem for path in destination.glob("*.torrent"))
    resume = decode((destination / f"{stem}.fastresume").read_bytes())
    assert isinstance(resume, dict)
    assert resume[b"info-hash"].hex() == stem
    assert resume[b"pieces"] == b"\x00\x00"
    assert resume[b"paused"] == 1
    assert resume[b"added_time"] == 1_600_000_123
    assert resume[b"qBt-category"] == b"fixture-category"
