"""Command-line interface for the migration package."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .migration import MigrationError, migrate


def _path_map(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("path map must be OLD=NEW")
    old, new = value.split("=", 1)
    if not old:
        raise argparse.ArgumentTypeError("path map OLD cannot be empty")
    return old, new


def _progress(message: str) -> None:
    print(f"[info] {message}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tixati-to-qbittorrent",
        description=(
            "Create qBittorrent .torrent/.fastresume pairs from a Tixati "
            "profile or exported torrent metadata."
        ),
    )
    parser.add_argument("tixati_dir", type=Path, help="Tixati config directory")
    parser.add_argument(
        "output_dir",
        type=Path,
        help="qBittorrent profile directory; BT_backup is created inside it",
    )
    parser.add_argument(
        "--torrent-source",
        type=Path,
        help="directory containing exported .torrent files (recursive)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="JSON manifest with per-torrent paths, categories and priorities",
    )
    parser.add_argument(
        "--save-path",
        help="default qBittorrent save path; blank lets qBittorrent use its default",
    )
    parser.add_argument("--category", default="", help="default qBittorrent category")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="default qBittorrent tag; may be repeated",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="write torrents unpaused; the default is stopped for a safe recheck",
    )
    parser.add_argument(
        "--content-layout",
        choices=("Original", "Subfolder"),
        default="Original",
    )
    parser.add_argument(
        "--path-map",
        type=_path_map,
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rewrite a save/download path prefix; may be repeated",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing target files atomically",
    )
    parser.add_argument(
        "--write-settings",
        action="store_true",
        help="also write qBittorrent.conf inside output_dir",
    )
    parser.add_argument(
        "--settings-path",
        type=Path,
        help="explicit path for qBittorrent.conf",
    )
    parser.add_argument("--incomplete-path", default="")
    parser.add_argument("--listen-port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = migrate(
            args.tixati_dir,
            args.output_dir,
            torrent_source=args.torrent_source,
            manifest=args.manifest,
            save_path=args.save_path,
            category=args.category,
            tags=args.tag,
            paused=not args.start,
            content_layout=args.content_layout,
            path_maps=args.path_map,
            overwrite=args.overwrite,
            write_settings=args.write_settings,
            settings_path=args.settings_path,
            incomplete_path=args.incomplete_path,
            listen_port=args.listen_port,
            progress=_progress,
        )
    except MigrationError as exc:
        print(f"error: {exc}")
        return 2
    print(f"wrote {report.torrent_count} torrent and {report.fastresume_count} fastresume files")
    print(f"output: {report.output_dir}")
    if report.skipped_count:
        print(
            f"skipped {report.skipped_count} Tixati hash-only/link records "
            "without reconstructable torrent piece metadata"
        )
        print("skipped info hashes: " + ", ".join(report.skipped_info_hashes))
    if report.settings_path is not None:
        print(f"settings: {report.settings_path}")
    return 0
