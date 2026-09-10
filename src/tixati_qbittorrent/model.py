"""Public data structures for migration inputs and results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TransferSpec:
    """One torrent plus qBittorrent-specific resume settings.

    ``piece_state`` is optional and contains one 0/1 value per torrent piece.
    When absent, the generated resume data marks every piece as unchecked so
    qBittorrent will verify the existing files instead of trusting stale state.
    ``added_time`` is an optional Unix timestamp used for qBittorrent's
    original transfer-added date.
    """

    torrent_path: Path
    save_path: str = ""
    download_path: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    paused: bool = True
    content_layout: str = "Original"
    file_priority: tuple[int, ...] | None = None
    piece_state: tuple[int, ...] | None = None
    added_time: int | None = None


@dataclass(frozen=True)
class MigrationReport:
    """Files produced by one migration run and records that were skipped."""

    torrent_count: int
    fastresume_count: int
    output_dir: Path
    settings_path: Path | None = None
    skipped_count: int = 0
    skipped_info_hashes: tuple[str, ...] = ()
