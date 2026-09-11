from __future__ import annotations

import hashlib
import json
from hashlib import sha1
from pathlib import Path
import struct

import pytest

from tixati_qbittorrent.bencode import BencodeError, decode, encode
from tixati_qbittorrent.migration import MigrationError, migrate
from tixati_qbittorrent.tixati_core import _rc4
from tixati_qbittorrent.tixati_settings import SETTINGS_KEY


def _write_torrent(path: Path, *, multi_file: bool = False) -> bytes:
    if multi_file:
        info = {
            b"files": [
                {b"length": 3, b"path": [b"one.bin"]},
                {b"length": 4, b"path": [b"two.bin"]},
            ],
            b"name": b"bundle",
            b"piece length": 4,
            b"pieces": b"a" * 40,
        }
    else:
        info = {
            b"length": 7,
            b"name": "café.bin".encode(),
            b"piece length": 4,
            b"pieces": b"a" * 40,
        }
    root = {
        b"announce": b"https://tracker.example/announce",
        b"announce-list": [[b"https://tracker.example/announce"]],
        b"info": info,
    }
    raw = encode(root)
    path.write_bytes(raw)
    return sha1(encode(info)).digest()


def _write_settings(path: Path, *, watched: bool = False) -> None:
    def entry(name: str, value_type: int, payload: bytes) -> bytes:
        key = name.encode("utf-8")
        return (
            struct.pack("<I", len(key) + 1)
            + b"\x02"
            + key
            + struct.pack("<I", len(payload) + 1)
            + bytes([value_type])
            + payload
        )

    values = [
        entry("incport", 3, struct.pack("<I", 15363)),
        entry("incpath", 12, b"\x02\x00\x00\x00\x05\x00\x00\x00\x02home"
              b"\x07\x00\x00\x00\x02kasumi"),
    ]
    if watched:
        values.append(
            entry("watchedfolder", 12, b"\x02\x00\x00\x00\x04\x00\x00\x00\x02srv"
                  b"\x06\x00\x00\x00\x02watch")
        )
    entries = b"".join(values)
    payload = struct.pack("<I", len(values)) + entries
    plaintext = struct.pack("<IB", len(payload) + 1, 11) + payload
    path.write_bytes(
        b"\x02\x00\x00\x00"
        + _rc4(plaintext, SETTINGS_KEY)
        + hashlib.sha1(plaintext).digest()
    )


def test_bencode_round_trip_and_canonical_order() -> None:
    value = {b"z": [b"x", 3], b"a": {b"nested": -4}}
    encoded = encode(value)
    assert encoded == b"d1:ad6:nestedi-4ee1:zl1:xi3eee"
    assert decode(encoded) == value
    with pytest.raises(BencodeError):
        decode(b"i01e")


def test_noncanonical_torrent_uses_raw_info_hash(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    torrents = tmp_path / "torrents"
    tixati.mkdir()
    torrents.mkdir()
    (tixati / "core2.dat").write_bytes(b"\x02\x00\x00\x00")
    info = (
        b"d4:name8:test.bin6:lengthi7e12:piece lengthi4e6:pieces40:"
        + b"a" * 40
        + b"e"
    )
    raw_torrent = b"d4:info" + info + b"e"
    torrent_path = torrents / "noncanonical.torrent"
    torrent_path.write_bytes(raw_torrent)
    expected_hash = sha1(info).hexdigest()

    migrate(tixati, tmp_path / "profile", torrent_source=torrents, now=1_700_000_000)

    assert (tmp_path / "profile" / "BT_backup" / f"{expected_hash}.torrent").read_bytes() == raw_torrent
    resume = decode(
        (tmp_path / "profile" / "BT_backup" / f"{expected_hash}.fastresume").read_bytes()
    )
    assert isinstance(resume, dict)
    assert resume[b"info-hash"].hex() == expected_hash


def test_migrate_recursive_torrents_and_fastresume(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    torrent_source = tmp_path / "exported" / "nested"
    tixati.mkdir()
    torrent_source.mkdir(parents=True)
    (tixati / "core2.dat").write_bytes(b"\x02\x00\x00\x00")
    info_hash = _write_torrent(torrent_source / "human-name.torrent")
    profile = tmp_path / "profile"
    destination = profile / "BT_backup"

    report = migrate(
        tixati,
        profile,
        torrent_source=torrent_source.parent,
        save_path="/srv/downloads",
        category="migrated",
        tags=["from-tixati"],
        now=1_700_000_000,
    )

    assert report.torrent_count == 1
    stem = info_hash.hex()
    assert (destination / f"{stem}.torrent").read_bytes() == (
        torrent_source / "human-name.torrent"
    ).read_bytes()
    resume = decode((destination / f"{stem}.fastresume").read_bytes())
    assert isinstance(resume, dict)
    assert resume[b"info-hash"] == info_hash
    assert resume[b"save_path"] == b"/srv/downloads"
    assert resume[b"qBt-category"] == b"migrated"
    assert resume[b"qBt-tags"] == [b"from-tixati"]
    assert resume[b"paused"] == 1
    assert resume[b"pieces"] == b"\x00\x00"
    assert resume[b"file_priority"] == [1]
    assert resume[b"added_time"] == 1_700_000_000
    assert (destination / f"{stem}.torrent").stat().st_mtime_ns == 1_700_000_000_000_000_000
    assert (destination / f"{stem}.fastresume").stat().st_mtime_ns == 1_700_000_000_000_000_000


def test_manifest_supports_path_mapping_and_known_state(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    torrent_path = metadata / "source.torrent"
    info_hash = _write_torrent(torrent_path, multi_file=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "torrents": [
                    {
                        "torrent": "metadata/source.torrent",
                        "save_path": r"C:\Downloads\Music",
                        "added_time": 1_600_000_000,
                        "file_priority": [1, 0],
                        "piece_state": [1, 0],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    destination = profile / "BT_backup"

    migrate(
        tixati,
        profile,
        manifest=manifest,
        path_maps=[(r"C:\Downloads", "/srv/downloads")],
        now=1_700_000_000,
    )

    resume = decode((destination / f"{info_hash.hex()}.fastresume").read_bytes())
    assert isinstance(resume, dict)
    assert resume[b"save_path"] == b"/srv/downloads/Music"
    assert resume[b"file_priority"] == [1, 0]
    assert resume[b"pieces"] == b"\x01\x00"
    assert resume[b"added_time"] == 1_600_000_000
    assert resume[b"qBt-seedStatus"] == 0
    assert (destination / f"{info_hash.hex()}.torrent").stat().st_mtime_ns == 1_600_000_000_000_000_000
    assert (destination / f"{info_hash.hex()}.fastresume").stat().st_mtime_ns == 1_600_000_000_000_000_000


def test_complete_piece_state_does_not_fabricate_completion_date(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    torrent_path = metadata / "complete.torrent"
    info_hash = _write_torrent(torrent_path, multi_file=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "torrents": [
                    {
                        "torrent": "metadata/complete.torrent",
                        "piece_state": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    migrate(tixati, tmp_path / "profile", manifest=manifest, now=1_700_000_000)

    resume = decode(
        (tmp_path / "profile" / "BT_backup" / f"{info_hash.hex()}.fastresume").read_bytes()
    )
    assert isinstance(resume, dict)
    assert resume[b"pieces"] == b"\x01\x01"
    assert resume[b"qBt-seedStatus"] == 1
    assert resume[b"completed_time"] == 0
    assert resume[b"last_seen_complete"] == 0


def test_missing_torrent_metadata_is_actionable(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    (tixati / "core2.dat").write_bytes(b"\x02\x00\x00\x00")
    with pytest.raises(MigrationError, match=r"Share > Save \.torrent"):
        migrate(tixati, tmp_path / "profile")


def test_optional_settings_are_written_next_to_backup(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    _write_settings(tixati / "settings2.dat")
    source = tmp_path / "torrents"
    source.mkdir()
    _write_torrent(source / "one.torrent")
    profile = tmp_path / "profile"
    destination = profile / "BT_backup"

    report = migrate(
        tixati,
        profile,
        torrent_source=source,
        save_path="/srv/downloads",
        write_settings=True,
    )

    assert report.settings_path == profile / "qBittorrent.conf"
    assert not list(profile.glob("*.torrent"))
    assert not list(profile.glob("*.fastresume"))
    settings = report.settings_path.read_text(encoding="utf-8")
    assert "Downloads\\SavePath=/srv/downloads" in settings
    assert "[BitTorrent]" in settings
    assert "Session\\Port=15363" in settings


def test_tixati_settings_are_imported_and_path_mapped(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    _write_settings(tixati / "settings2.dat")
    source = tmp_path / "torrents"
    source.mkdir()
    _write_torrent(source / "one.torrent")

    report = migrate(
        tixati,
        tmp_path / "profile",
        torrent_source=source,
        path_maps=[("/home/kasumi", "/srv")],
        write_settings=True,
    )

    settings = report.settings_path.read_text(encoding="utf-8")
    assert "Downloads\\SavePath=/srv" in settings
    assert "Session\\DefaultSavePath=/srv" in settings
    assert "Session\\Port=15363" in settings


def test_named_watched_folder_is_written_in_qb_schema(tmp_path: Path) -> None:
    tixati = tmp_path / "tixati"
    tixati.mkdir()
    _write_settings(tixati / "settings2.dat", watched=True)
    source = tmp_path / "torrents"
    source.mkdir()
    _write_torrent(source / "one.torrent")

    report = migrate(
        tixati,
        tmp_path / "profile",
        torrent_source=source,
        write_settings=True,
    )

    assert report.watched_folders_path == tmp_path / "profile" / "watched_folders.json"
    watched = json.loads(report.watched_folders_path.read_text(encoding="utf-8"))
    assert watched["/srv/watch"]["add_torrent_params"]["use_auto_tmm"] is True
    assert report.metadata_path == tmp_path / "profile" / "tixati-migration.json"
