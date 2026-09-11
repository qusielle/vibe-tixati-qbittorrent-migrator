from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from tixati_qbittorrent.tixati_core import _rc4
from tixati_qbittorrent.config import render_qbittorrent_conf
from tixati_qbittorrent.tixati_settings import (
    SETTINGS_KEY,
    SettingsFormatError,
    TixatiSetting,
    TixatiSettings,
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
            _entry("incportupnp", 3, struct.pack("<I", 1)),
            _entry("incpath", 12, _path("home", "kasumi", "Downloads")),
            _entry("xferdefaultdhtmode", 3, struct.pack("<I", 2)),
            _entry("maxpeerconn", 3, struct.pack("<I", 200)),
            _entry("ulslots", 3, struct.pack("<I", 100)),
            _entry("dlslots", 3, struct.pack("<I", 20)),
            _entry("xferdlslotmanage", 3, struct.pack("<I", 1)),
            _entry("xferulslotmanage", 3, struct.pack("<I", 1)),
            _entry(0x12345678, 3, struct.pack("<I", 1)),
        )
    )
    payload = struct.pack("<I", 10) + entries
    plaintext = struct.pack("<IB", len(payload) + 1, 11) + payload
    raw = b"\x02\x00\x00\x00" + _rc4(plaintext, SETTINGS_KEY)
    path.write_bytes(raw + hashlib.sha1(plaintext).digest())


def test_settings_reader_decodes_named_and_numeric_entries(tmp_path: Path) -> None:
    source = tmp_path / "settings2.dat"
    _settings_file(source)

    settings = read_settings(source)

    assert len(settings.entries) == 10
    assert settings.get("incport") == 15363
    assert settings.path("incpath") == "/home/kasumi/Downloads"
    numeric = settings.entries[-1]
    assert numeric.field_id == 0x12345678
    assert numeric.value == 1


def test_qbittorrent_settings_use_tixati_defaults(tmp_path: Path) -> None:
    source = tmp_path / "settings2.dat"
    _settings_file(source)
    from tixati_qbittorrent.tixati_settings import read_settings

    settings = read_settings(source)

    rendered = render_qbittorrent_conf(tixati_settings=settings)

    assert "Downloads\\SavePath=/home/kasumi/Downloads" in rendered
    assert "[BitTorrent]" in rendered
    assert "Session\\Port=15363" in rendered
    assert "Session\\DHTEnabled=true" in rendered
    assert "Session\\MaxConnectionsPerTorrent=200" in rendered
    assert "Session\\MaxUploads=100" in rendered
    assert "Session\\MaxActiveDownloads=20" in rendered
    assert "Session\\MaxActiveUploads=100" in rendered
    assert "[Network]" in rendered
    assert "PortForwardingEnabled=true" in rendered


def test_nested_proxy_registry_and_network_mappings() -> None:
    def typed(value_type: int, payload: bytes) -> bytes:
        return struct.pack("<I", len(payload) + 1) + bytes([value_type]) + payload

    def pair(name: str, value_type: int, payload: bytes) -> bytes:
        key = name.encode("utf-8")
        return typed(2, key) + typed(value_type, payload)

    proxy_map = struct.pack("<I", 6) + b"".join(
        (
            pair("pproxyaddress", 2, b"127.0.0.1"),
            pair("pproxylogin", 2, b"alice"),
            pair("pproxymode", 3, struct.pack("<I", 3)),
            pair("pproxypass", 2, b"secret"),
            pair("pproxyport", 2, b"12345"),
            pair("pproxyssl", 3, struct.pack("<I", 0)),
        )
    )
    proxy_list = struct.pack("<I", 1) + typed(11, proxy_map)
    settings = TixatiSettings(
        (
            TixatiSetting("peercryptmode", None, 3, struct.pack("<I", 3)),
            TixatiSetting("incconncryptmode", None, 3, struct.pack("<I", 2)),
            TixatiSetting("peerconnproto", None, 3, struct.pack("<I", 3)),
            TixatiSetting("filealloctype", None, 3, struct.pack("<I", 2)),
            TixatiSetting("local_ip", None, 2, b"tun0"),
            TixatiSetting("readthrottle_mode", None, 3, struct.pack("<I", 0)),
            TixatiSetting("readthrottle_rate", None, 3, struct.pack("<I", 512000)),
            TixatiSetting("writethrottle_mode", None, 3, struct.pack("<I", 0)),
            TixatiSetting("writethrottle_rate", None, 3, struct.pack("<I", 100000)),
            TixatiSetting(None, 0x25203E0C, 12, proxy_list),
        )
    )

    proxy = settings.field_map(0x25203E0C)
    assert proxy["pproxymode"].value == 3
    assert proxy["pproxyaddress"].value == "127.0.0.1"

    rendered = render_qbittorrent_conf(tixati_settings=settings)

    assert "Session\\Encryption=1" in rendered
    assert "Session\\BTProtocol=Both" in rendered
    assert "Session\\Preallocation=true" in rendered
    assert "Session\\Interface=tun0" in rendered
    assert "Proxy\\Type=SOCKS5" in rendered
    assert "Proxy\\AuthEnabled=true" in rendered
    assert "ReadThrottleRate=512000" in rendered
    assert "WriteThrottleRate=100000" in rendered


def test_watched_folder_paths_ignore_recent_folder_history() -> None:
    settings = TixatiSettings(
        (
            TixatiSetting("mrufolders", None, 12, _path("home", "recent")),
            TixatiSetting("watchedfolder", None, 12, _path("srv", "watch")),
        )
    )

    assert settings.watched_folder_paths() == ("/srv/watch",)


def test_settings_reader_rejects_bad_integrity(tmp_path: Path) -> None:
    source = tmp_path / "settings2.dat"
    _settings_file(source)
    data = bytearray(source.read_bytes())
    data[-1] ^= 0x01
    source.write_bytes(data)

    with pytest.raises(SettingsFormatError, match="integrity"):
        read_settings(source)
