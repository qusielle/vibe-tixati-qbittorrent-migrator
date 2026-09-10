"""Optional qBittorrent profile settings writer."""

from __future__ import annotations

from pathlib import Path


def render_qbittorrent_conf(
    *,
    save_path: str = "",
    incomplete_path: str = "",
    listen_port: int | None = None,
) -> str:
    """Render a small qBittorrent ``QSettings``-compatible configuration.

    qBittorrent keeps this file next to its ``BT_backup`` directory.  Only
    settings that are unambiguous and useful for a migration are emitted;
    Tixati's proprietary settings database is not translated to qBittorrent's
    unrelated preference keys.
    """

    lines = [
        "[Preferences]",
        f"Downloads\\SavePath={save_path}",
        f"Downloads\\TempPath={incomplete_path}",
        f"Downloads\\TempPathEnabled={'true' if incomplete_path else 'false'}",
    ]
    if listen_port is not None:
        if not 1 <= listen_port <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
        lines.append(f"BitTorrent\\Session\\Port={listen_port}")
    return "\n".join(lines) + "\n"


def write_qbittorrent_conf(
    path: Path,
    *,
    save_path: str = "",
    incomplete_path: str = "",
    listen_port: int | None = None,
) -> None:
    """Write a qBittorrent config file, creating its parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_qbittorrent_conf(
            save_path=save_path,
            incomplete_path=incomplete_path,
            listen_port=listen_port,
        ),
        encoding="utf-8",
    )

