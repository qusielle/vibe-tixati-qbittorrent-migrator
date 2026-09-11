"""Optional qBittorrent profile settings writer."""

from __future__ import annotations

from pathlib import Path

from .tixati_settings import TixatiSetting, TixatiSettings


_PROXY_FIELD_ID = 0x25203E0C


def _setting_text(settings: TixatiSettings, name: str) -> str | None:
    value = settings.get(name)
    return value if isinstance(value, str) else None


def _nested_text(values: dict[str, TixatiSetting], name: str) -> str | None:
    value = values.get(name)
    if value is None or not isinstance(value.value, str):
        return None
    return value.value


def _nested_int(values: dict[str, TixatiSetting], name: str) -> int | None:
    value = values.get(name)
    if value is None or not isinstance(value.value, int):
        return None
    return value.value


def _append_source_value(
    lines: list[str], settings: TixatiSettings, source_name: str, target_name: str
) -> None:
    """Retain an unmapped source value in the self-documenting side section."""

    entry = settings.get_entry(source_name)
    if entry is None:
        return
    value = entry.value
    if isinstance(value, (str, int, float)):
        lines.append(f"{target_name}={value}")
    elif isinstance(value, bytes):
        lines.append(f"{target_name}Hex={value.hex()}")


def render_qbittorrent_conf(
    *,
    save_path: str = "",
    incomplete_path: str = "",
    tixati_settings: TixatiSettings | None = None,
) -> str:
    """Render a qBittorrent ``QSettings``-compatible configuration.

    Explicit arguments win over settings read from Tixati.  The source values
    are deliberately mapped one by one: qBittorrent has no exact equivalent
    for several Tixati controls, so guessing would be worse than leaving those
    controls at qBittorrent's defaults.
    """

    source_save_path = tixati_settings.path("incpath") if tixati_settings else None
    effective_save_path = save_path or source_save_path or ""
    effective_port = (
        _int_setting(tixati_settings, "incport")
        if tixati_settings is not None
        else None
    )

    lines = [
        "[Preferences]",
        f"Downloads\\SavePath={effective_save_path}",
        f"Downloads\\TempPath={incomplete_path}",
        f"Downloads\\TempPathEnabled={'true' if incomplete_path else 'false'}",
    ]
    has_bittorrent_section = bool(effective_save_path)
    if effective_save_path:
        # Keep both the legacy Preferences key and the current session key;
        # qBittorrent versions in the wild have used both spellings.
        lines.extend(("", "[BitTorrent]", f"Session\\DefaultSavePath={effective_save_path}"))

    session_values: list[str] = []
    network_values: list[str] = []
    if incomplete_path:
        session_values.extend(
            (
                f"Session\\TempPath={incomplete_path}",
                "Session\\TempPathEnabled=true",
            )
        )
    if effective_port is not None:
        if not 1 <= effective_port <= 65535:
            raise ValueError("Tixati incport must be between 1 and 65535")
        session_values.append(f"Session\\Port={effective_port}")

    if tixati_settings is not None:
        _append_bool_setting(
            session_values,
            tixati_settings,
            "xferdefaultdhtmode",
            "Session\\DHTEnabled",
        )
        _append_int_setting(
            session_values,
            tixati_settings,
            "maxpeerconn",
            "Session\\MaxConnectionsPerTorrent",
        )
        _append_int_setting(
            session_values,
            tixati_settings,
            "ulslots",
            "Session\\MaxUploads",
        )
        if _int_setting(tixati_settings, "xferdlslotmanage"):
            _append_int_setting(
                session_values,
                tixati_settings,
                "dlslots",
                "Session\\MaxActiveDownloads",
            )
        if _int_setting(tixati_settings, "xferulslotmanage"):
            _append_int_setting(
                session_values,
                tixati_settings,
                "ulslots",
                "Session\\MaxActiveUploads",
            )

        protocol = _int_setting(tixati_settings, "peerconnproto")
        if protocol is None:
            protocol = _int_setting(tixati_settings, "incpeerconnproto")
        protocol_name = {1: "TCP", 2: "uTP", 3: "Both"}.get(protocol or 0)
        if protocol_name is not None:
            session_values.append(f"Session\\BTProtocol={protocol_name}")

        allocation = _int_setting(tixati_settings, "filealloctype")
        if allocation in (1, 2, 3):
            # Tixati 1 is full pre-write, 2 is fast allocation and 3 is
            # sparse. qBittorrent exposes the same useful distinction as a
            # preallocation boolean; it has no separate fast-allocation mode.
            session_values.append(
                f"Session\\Preallocation={'true' if allocation in (1, 2) else 'false'}"
            )

        encryption = _encryption_mode(tixati_settings)
        if encryption is not None:
            session_values.append(f"Session\\Encryption={encryption}")

        interface = _setting_text(tixati_settings, "local_ip")
        if interface:
            # qBittorrent versions have used both names. Keeping the pair
            # makes the generated profile useful across supported releases;
            # an interface name such as tun0 is intentionally not treated as
            # an IP address.
            session_values.extend(
                (
                    f"Session\\Interface={interface}",
                    f"Session\\InterfaceName={interface}",
                )
            )

        migration_values: list[str] = []
        for source_name, target_name in (
            ("readthrottle_mode", "ReadThrottleMode"),
            ("readthrottle_rate", "ReadThrottleRate"),
            ("writethrottle_mode", "WriteThrottleMode"),
            ("writethrottle_rate", "WriteThrottleRate"),
            ("ipmode", "IPMode"),
            ("local_ip", "LocalIP"),
            ("local_ip6", "LocalIP6"),
            ("peercryptmode", "PeerCryptMode"),
            ("incconncryptmode", "IncomingCryptMode"),
            ("peerconnproto", "PeerConnectionProtocol"),
            ("incpeerconnproto", "IncomingPeerConnectionProtocol"),
            ("filealloctype", "FileAllocationType"),
        ):
            _append_source_value(
                migration_values,
                tixati_settings,
                source_name,
                target_name,
            )
        bwquotas = tixati_settings.get_entry("bwquotas")
        if bwquotas is not None and isinstance(bwquotas.value, bytes):
            migration_values.append(f"BandwidthQuotasHex={bwquotas.value.hex()}")
        if migration_values:
            lines.extend(("", "[TixatiMigration]", *migration_values))

        network_values.extend(_proxy_values(tixati_settings))
    if session_values:
        if not has_bittorrent_section:
            lines.extend(("", "[BitTorrent]"))
            has_bittorrent_section = True
        lines.extend(session_values)

    if tixati_settings is not None:
        upnp = _int_setting(tixati_settings, "incportupnp")
        if upnp is not None:
            network_values.append(f"PortForwardingEnabled={'true' if upnp else 'false'}")
        if network_values:
            lines.extend(("", "[Network]", *network_values))
    return "\n".join(lines) + "\n"


def write_qbittorrent_conf(
    path: Path,
    *,
    save_path: str = "",
    incomplete_path: str = "",
    tixati_settings: TixatiSettings | None = None,
) -> None:
    """Write a qBittorrent config file, creating its parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_qbittorrent_conf(
            save_path=save_path,
            incomplete_path=incomplete_path,
            tixati_settings=tixati_settings,
        ),
        encoding="utf-8",
    )


def _int_setting(settings: TixatiSettings, name: str) -> int | None:
    value = settings.get(name)
    return value if isinstance(value, int) else None


def _encryption_mode(settings: TixatiSettings) -> int | None:
    """Map Tixati's outgoing/incoming policy pair to qB's one policy.

    In the observed v2 settings, value 1 is the unencrypted-only end of the
    policy scale and value 3 is the encrypted-only end. qBittorrent uses
    ``0`` for prefer encryption/allow either, ``1`` for require encryption and
    ``2`` for disable encryption. If only one side is present, that side is
    still useful; otherwise the conservative qB default is selected.
    """

    outgoing = _int_setting(settings, "peercryptmode")
    incoming = _int_setting(settings, "incconncryptmode")
    if outgoing is None and incoming is None:
        return None
    if outgoing == 3 or incoming == 3:
        return 1
    if outgoing == 1 and incoming == 1:
        return 2
    return 0


def _proxy_values(settings: TixatiSettings) -> list[str]:
    """Map Tixati's peer proxy profile to qBittorrent's BitTorrent proxy."""

    values = settings.field_map(_PROXY_FIELD_ID)
    mode = _nested_int(values, "pproxymode")
    proxy_type = {1: "SOCKS4", 2: "SOCKS4", 3: "SOCKS5", 4: "HTTP"}.get(mode or 0)
    address = _nested_text(values, "pproxyaddress")
    port = _nested_text(values, "pproxyport")
    if proxy_type is None or not address or not port:
        return []
    result = [
        f"Proxy\\IP={address}",
        f"Proxy\\Port={port}",
        f"Proxy\\Type={proxy_type}",
        "Proxy\\Profiles\\BitTorrent=true",
        "Proxy\\Profiles\\RSS=false",
        "Proxy\\Profiles\\Misc=false",
    ]
    login = _nested_text(values, "pproxylogin") or ""
    password = _nested_text(values, "pproxypass") or ""
    if login or password:
        result.extend(
            (
                "Proxy\\AuthEnabled=true",
                f"Proxy\\Username={login}",
                f"Proxy\\Password={password}",
            )
        )
    return result


def _append_int_setting(
    lines: list[str], settings: TixatiSettings, source_name: str, target_name: str
) -> None:
    value = _int_setting(settings, source_name)
    if value is not None:
        lines.append(f"{target_name}={value}")


def _append_bool_setting(
    lines: list[str], settings: TixatiSettings, source_name: str, target_name: str
) -> None:
    value = _int_setting(settings, source_name)
    if value is not None:
        lines.append(f"{target_name}={'true' if value else 'false'}")
