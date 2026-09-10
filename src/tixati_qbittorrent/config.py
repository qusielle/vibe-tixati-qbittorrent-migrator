"""Optional qBittorrent profile settings writer."""

from __future__ import annotations

from pathlib import Path

from .tixati_settings import TixatiSettings


def render_qbittorrent_conf(
    *,
    save_path: str = "",
    incomplete_path: str = "",
    listen_port: int | None = None,
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
    effective_port = listen_port
    if effective_port is None and tixati_settings is not None:
        effective_port = _int_setting(tixati_settings, "incport")

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
    if incomplete_path:
        session_values.extend(
            (
                f"Session\\TempPath={incomplete_path}",
                "Session\\TempPathEnabled=true",
            )
        )
    if effective_port is not None:
        if not 1 <= effective_port <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
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
    if session_values:
        if not has_bittorrent_section:
            lines.extend(("", "[BitTorrent]"))
            has_bittorrent_section = True
        lines.extend(session_values)

    if tixati_settings is not None:
        upnp = _int_setting(tixati_settings, "incportupnp")
        if upnp is not None:
            lines.extend(("", "[Network]", f"PortForwardingEnabled={'true' if upnp else 'false'}"))
    return "\n".join(lines) + "\n"


def write_qbittorrent_conf(
    path: Path,
    *,
    save_path: str = "",
    incomplete_path: str = "",
    listen_port: int | None = None,
    tixati_settings: TixatiSettings | None = None,
) -> None:
    """Write a qBittorrent config file, creating its parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_qbittorrent_conf(
            save_path=save_path,
            incomplete_path=incomplete_path,
            listen_port=listen_port,
            tixati_settings=tixati_settings,
        ),
        encoding="utf-8",
    )


def _int_setting(settings: TixatiSettings, name: str) -> int | None:
    value = settings.get(name)
    return value if isinstance(value, int) else None


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
