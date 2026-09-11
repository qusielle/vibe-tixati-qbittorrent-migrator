"""Build a qBittorrent ``BT_backup`` directory from torrent metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bencode import BValue, BencodeError, decode, encode, raw_dict_value
from .config import render_qbittorrent_conf
from .model import CategorySpec, MigrationReport, TransferSpec
from .tixati_core import (
    CoreCategory,
    CoreExtraction,
    CoreFormatError,
    CoreTransfer,
    Progress,
    extract_core_file,
)
from .tixati_settings import SettingsFormatError, TixatiSettings, read_settings


DEFAULT_LIBTORRENT_VERSION = "1.2.19.0"
_TIXATI_DATA_FILES = {
    "core2.dat",
    "settings2.dat",
    "uisettings2.dat",
    "autothrottle2.dat",
    "bwpresets2.dat",
    "channels2.dat",
    "colors2.dat",
    "dht2.dat",
    "ipfilters2.dat",
    "rss2.dat",
    "scheduler2.dat",
}


class MigrationError(RuntimeError):
    """Raised when the source is incomplete or cannot be converted safely."""


def _notify(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


@dataclass(frozen=True)
class _Torrent:
    source_path: Path
    raw: bytes
    root: dict[bytes, BValue]
    info: dict[bytes, BValue]
    info_hash: bytes
    piece_count: int
    file_count: int


@dataclass(frozen=True)
class _Prepared:
    torrent: _Torrent
    spec: TransferSpec
    fastresume: bytes


def _text(value: Any, *, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"{field} is not valid UTF-8") from exc
    raise MigrationError(f"{field} must be a string")


def _dict(value: Any, *, field: str) -> dict[bytes, BValue]:
    if not isinstance(value, dict):
        raise MigrationError(f"{field} must be a bencode dictionary")
    return value


def _parse_torrent(
    path: Path,
    raw: bytes,
    *,
    expected_info_hash: bytes | None = None,
) -> _Torrent:
    # `expected_info_hash` is used only for reconstructed core records. Their
    # raw info bytes have already been SHA-1 verified; decoding them here gives
    # us the normal qBittorrent validation fields without re-encoding the info
    # dictionary and accidentally changing a noncanonical hash.
    try:
        root_value = decode(raw)
    except BencodeError as exc:
        raise MigrationError(f"cannot read torrent {path}: {exc}") from exc
    root = _dict(root_value, field=f"{path} root")
    info = _dict(root.get(b"info"), field=f"{path} info")

    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes):
        raise MigrationError(
            f"{path} is v2-only or has no v1 piece hashes; "
            "this converter currently requires a v1-compatible .torrent"
        )
    if len(pieces) == 0 or len(pieces) % 20:
        raise MigrationError(f"{path} has an invalid v1 pieces field")

    if isinstance(info.get(b"files"), list):
        file_count = len(info[b"files"])
    else:
        file_count = 1
    if file_count < 1:
        raise MigrationError(f"{path} contains no files")

    try:
        raw_info = raw_dict_value(raw, b"info")
    except BencodeError as exc:
        raise MigrationError(f"cannot locate raw info dictionary in {path}: {exc}") from exc
    actual_info_hash = hashlib.sha1(raw_info).digest()
    if expected_info_hash is not None and actual_info_hash != expected_info_hash:
        raise MigrationError(
            f"{path} info hash mismatch: expected {expected_info_hash.hex()}, "
            f"got {actual_info_hash.hex()}"
        )
    info_hash = expected_info_hash or actual_info_hash
    return _Torrent(
        source_path=path,
        raw=raw,
        root=root,
        info=info,
        info_hash=info_hash,
        piece_count=len(pieces) // 20,
        file_count=file_count,
    )


def _load_torrent(path: Path) -> _Torrent:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"cannot read torrent {path}: {exc}") from exc
    return _parse_torrent(path, raw)


def _core_torrent(item: CoreTransfer) -> _Torrent:
    """Turn one reverse-engineered core record into qB torrent metadata."""

    # Kept as a small adapter instead of teaching the bencode writer about
    # Tixati's private structures.
    source_text = item.torrent_path or "core2.dat"
    source_path = Path(source_text)
    return _parse_torrent(
        source_path,
        item.torrent_bytes,
        expected_info_hash=item.info_hash,
    )


def _tracker_tiers(root: dict[bytes, BValue]) -> list[list[bytes]]:
    announce_list = root.get(b"announce-list")
    if isinstance(announce_list, list):
        tiers: list[list[bytes]] = []
        for tier in announce_list:
            if isinstance(tier, list):
                urls = [url for url in tier if isinstance(url, bytes)]
                if urls:
                    tiers.append(urls)
        if tiers:
            return tiers
    announce = root.get(b"announce")
    if isinstance(announce, bytes):
        return [[announce]]
    return []


def _url_list(root: dict[bytes, BValue]) -> list[bytes]:
    urls = root.get(b"url-list")
    if isinstance(urls, bytes):
        return [urls]
    if isinstance(urls, list):
        return [url for url in urls if isinstance(url, bytes)]
    return []


def _apply_path_maps(path: str, path_maps: Iterable[tuple[str, str]]) -> str:
    if not path:
        return path
    normalized = path.replace("\\", "/")
    for old, new in path_maps:
        old_normalized = old.replace("\\", "/").rstrip("/")
        if not old_normalized:
            continue
        if normalized == old_normalized or normalized.startswith(old_normalized + "/"):
            suffix = normalized[len(old_normalized) :]
            return new.rstrip("/") + suffix
    return path


def _validate_piece_state(state: tuple[int, ...] | None, count: int) -> bytes:
    if state is None:
        return b"\x00" * count
    if len(state) != count:
        raise MigrationError(
            f"piece_state has {len(state)} entries, expected {count}"
        )
    if any(piece not in (0, 1) for piece in state):
        raise MigrationError("piece_state entries must be 0 or 1")
    return bytes(state)


def _make_fastresume(
    torrent: _Torrent,
    spec: TransferSpec,
    *,
    now: int,
) -> bytes:
    # qBittorrent/libtorrent accepts a compact 0/1 piece-state byte string.
    # Unknown Tixati state is intentionally represented as all zeroes by
    # `_validate_piece_state`, which causes a safe recheck on first load.
    if spec.file_priority is None:
        file_priority = [1] * torrent.file_count
    else:
        if len(spec.file_priority) != torrent.file_count:
            raise MigrationError(
                f"file_priority has {len(spec.file_priority)} entries for "
                f"{torrent.file_count} torrent files ({torrent.source_path})"
            )
        if any(priority not in range(8) for priority in spec.file_priority):
            raise MigrationError("file_priority entries must be between 0 and 7")
        file_priority = list(spec.file_priority)

    pieces = _validate_piece_state(spec.piece_state, torrent.piece_count)
    complete = bool(pieces) and all(piece == 1 for piece in pieces)
    save_path = _apply_path_maps(spec.save_path, ())
    download_path = _apply_path_maps(spec.download_path, ())
    private = torrent.info.get(b"private") == 1
    # Direct core2.dat migrations provide Tixati's original Created/Added
    # timestamp. Exported .torrent files and manifests normally do not, so
    # their established behavior remains to use the migration timestamp.
    added_time = spec.added_time if spec.added_time is not None else now
    # Tixati exposes one later per-transfer activity/state-change timestamp,
    # not separate upload and download event timestamps.  A complete torrent
    # necessarily downloaded its final data at that time, so it is safe to
    # use it for qB's completion and last-download fields.  For an incomplete
    # transfer it remains useful as the last observed download/activity time;
    # last-upload is left at zero because it cannot be distinguished from it.
    completed_time = spec.completed_time if complete else None
    if completed_time is not None and completed_time < 0:
        completed_time = None
    finished_time = (
        max(0, now - completed_time)
        if completed_time is not None
        else 0
    )
    last_download = spec.last_download or 0
    last_upload = spec.last_upload or 0
    last_seen_complete = spec.last_seen_complete or 0

    resume: dict[bytes, BValue] = {
        b"active_time": spec.active_time,
        b"added_time": added_time,
        b"allocation": b"sparse",
        b"apply_ip_filter": 1,
        b"auto_managed": 0 if spec.paused else 1,
        b"completed_time": completed_time or 0,
        b"disable_dht": 1 if private else 0,
        b"disable_lsd": 1 if private else 0,
        b"disable_pex": 1 if private else 0,
        b"download_rate_limit": -1,
        b"file-format": b"libtorrent resume file",
        b"file-version": 1,
        b"file_priority": file_priority,
        b"finished_time": finished_time,
        b"httpseeds": [],
        b"info-hash": torrent.info_hash,
        b"last_download": last_download,
        b"last_seen_complete": last_seen_complete,
        b"last_upload": last_upload,
        b"libtorrent-version": DEFAULT_LIBTORRENT_VERSION.encode("ascii"),
        b"max_connections": 16777215,
        b"max_uploads": 16777215,
        b"num_complete": 0,
        b"num_downloaded": 0,
        b"num_incomplete": 0,
        b"paused": 1 if spec.paused else 0,
        b"piece_priority": b"\x01" * torrent.piece_count,
        b"pieces": pieces,
        b"qBt-category": spec.category.encode("utf-8"),
        b"qBt-contentLayout": spec.content_layout.encode("utf-8"),
        b"qBt-downloadPath": download_path.encode("utf-8"),
        b"qBt-firstLastPiecePriority": 0,
        b"qBt-inactiveSeedingTimeLimit": -2,
        b"qBt-name": b"",
        b"qBt-ratioLimit": spec.ratio_limit,
        b"qBt-savePath": save_path.encode("utf-8"),
        b"qBt-seedStatus": 1 if complete else 0,
        b"qBt-seedingTimeLimit": spec.seeding_time_limit,
        b"qBt-stopCondition": b"None",
        b"qBt-tags": [tag.encode("utf-8") for tag in spec.tags],
        b"save_path": save_path.encode("utf-8"),
        b"seed_mode": 0,
        b"seeding_time": spec.seeding_time,
        b"sequential_download": 0,
        b"share_mode": 0,
        b"stop_when_ready": 0,
        b"super_seeding": 0,
        b"total_downloaded": spec.total_downloaded,
        b"total_uploaded": spec.total_uploaded,
        b"trackers": _tracker_tiers(torrent.root),
        b"upload_mode": 0,
        b"upload_rate_limit": -1,
        b"url-list": _url_list(torrent.root),
    }
    return encode(resume)


def _parse_tags(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MigrationError(f"{field} must be a list of strings")
    return tuple(value)


def _parse_int_tuple(value: Any, *, field: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value
    ):
        raise MigrationError(f"{field} must be a list of integers")
    return tuple(value)


def _parse_timestamp(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MigrationError(f"{field} must be a non-negative integer")
    return value


def _manifest_specs(
    manifest_path: Path,
    data: Any,
    *,
    cli_save_path: str | None,
    cli_category: str,
    cli_tags: tuple[str, ...],
    cli_paused: bool,
    cli_content_layout: str,
    path_maps: Iterable[tuple[str, str]],
) -> list[TransferSpec]:
    if not isinstance(data, dict):
        raise MigrationError("manifest must contain a JSON object")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise MigrationError("manifest defaults must be an object")
    entries = data.get("torrents")
    if not isinstance(entries, list) or not entries:
        raise MigrationError("manifest torrents must be a non-empty list")

    specs: list[TransferSpec] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise MigrationError(f"manifest torrent #{index} must be an object")
        torrent_value = entry.get("torrent")
        if not isinstance(torrent_value, str):
            raise MigrationError(f"manifest torrent #{index} needs a torrent path")
        torrent_path = Path(torrent_value)
        if not torrent_path.is_absolute():
            torrent_path = (manifest_path.parent / torrent_path).resolve()

        def setting(name: str, default: Any = None) -> Any:
            return entry[name] if name in entry else defaults.get(name, default)

        save_path = cli_save_path if cli_save_path is not None else setting("save_path", "")
        category = setting("category", cli_category)
        tags = setting("tags", list(cli_tags))
        paused = setting("paused", cli_paused)
        content_layout = setting("content_layout", cli_content_layout)
        download_path = setting("download_path", "")
        if not isinstance(save_path, str) or not isinstance(download_path, str):
            raise MigrationError(f"manifest torrent #{index} paths must be strings")
        if not isinstance(category, str) or not isinstance(content_layout, str):
            raise MigrationError(f"manifest torrent #{index} category/layout must be strings")
        if not isinstance(paused, bool):
            raise MigrationError(f"manifest torrent #{index} paused must be boolean")
        specs.append(
            TransferSpec(
                torrent_path=torrent_path,
                save_path=_apply_path_maps(save_path, path_maps),
                download_path=_apply_path_maps(download_path, path_maps),
                category=category,
                tags=_parse_tags(tags, field=f"manifest torrent #{index} tags"),
                paused=paused,
                content_layout=content_layout,
                file_priority=_parse_int_tuple(
                    setting("file_priority"),
                    field=f"manifest torrent #{index} file_priority",
                ),
                piece_state=_parse_int_tuple(
                    setting("piece_state"),
                    field=f"manifest torrent #{index} piece_state",
                ),
                added_time=_parse_timestamp(
                    setting("added_time"),
                    field=f"manifest torrent #{index} added_time",
                ),
            )
        )
    return specs


def _default_specs(
    torrent_source: Path,
    *,
    save_path: str | None,
    category: str,
    tags: tuple[str, ...],
    paused: bool,
    content_layout: str,
    path_maps: Iterable[tuple[str, str]],
) -> list[TransferSpec]:
    paths = sorted(
        (path for path in torrent_source.rglob("*") if path.is_file() and path.suffix.lower() == ".torrent"),
        key=lambda path: str(path).casefold(),
    )
    if not paths:
        available = sorted(
            path.name for path in torrent_source.iterdir() if path.is_file()
        )
        tixati_files = sorted(name for name in available if name in _TIXATI_DATA_FILES)
        hint = (
            " The directory contains Tixati's proprietary core2.dat, but no "
            ".torrent metadata. Select all transfers in Tixati and use "
            "Share > Save .torrent, then pass that directory with "
            "--torrent-source."
            if "core2.dat" in tixati_files
            else ""
        )
        raise MigrationError(
            f"no .torrent files found under {torrent_source}.{hint}"
        )
    return [
        TransferSpec(
            torrent_path=path,
            save_path=_apply_path_maps(save_path or "", path_maps),
            category=category,
            tags=tags,
            paused=paused,
            content_layout=content_layout,
        )
        for path in paths
    ]


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    overwrite: bool,
    mtime: int | None = None,
) -> None:
    # Write, flush, fsync, and rename in the destination directory. A partially
    # written resume file is worse than a failed migration because qBittorrent
    # may discover it on its next startup.
    if path.exists() and not overwrite:
        raise MigrationError(f"refusing to overwrite existing file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if mtime is not None:
                # Set the timestamp on the temporary inode before replacing
                # the destination, so readers never observe a newly-created
                # output file with the wrong date.
                os.utime(temporary, (mtime, mtime))
                os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise MigrationError(f"cannot write {path}: {exc}") from exc


def _category_specs(
    prepared: Iterable[_Prepared],
    *,
    core_categories: Iterable[CoreCategory] | None,
    cli_save_path: str | None,
    path_maps: Iterable[tuple[str, str]],
) -> tuple[CategorySpec, ...]:
    """Build deterministic qBittorrent category registry entries.

    Direct core migrations use the complete Tixati user-category registry, so
    categories with no currently reconstructable transfer are retained too.
    Exported-torrent and manifest migrations have no global Tixati category
    map; in that case only categories actually assigned to prepared entries
    can be represented.
    """

    definitions: dict[str, CategorySpec] = {}
    if core_categories is not None:
        override = (
            _apply_path_maps(cli_save_path, path_maps)
            if cli_save_path is not None
            else None
        )
        for category in core_categories:
            definitions[category.name] = CategorySpec(
                name=category.name,
                save_path=(
                    override
                    if override is not None
                    else _apply_path_maps(category.save_path or "", path_maps)
                ),
            )

    for item in prepared:
        name = item.spec.category
        if not name:
            continue
        candidate = CategorySpec(
            name=name,
            save_path=item.spec.save_path,
            download_path=item.spec.download_path or None,
        )
        existing = definitions.get(name)
        if existing is None:
            definitions[name] = candidate
        elif existing != candidate and core_categories is None:
            raise MigrationError(
                f"category {name!r} has conflicting paths in the migration input"
            )

    return tuple(definitions[name] for name in sorted(definitions, key=str.casefold))


def _render_categories(categories: Iterable[CategorySpec]) -> bytes:
    """Render the current qBittorrent ``categories.json`` schema."""

    payload: dict[str, dict[str, str]] = {}
    for category in categories:
        item = {"name": category.name, "save_path": category.save_path}
        if category.download_path:
            item["download_path"] = category.download_path
        payload[category.name] = item
    return (json.dumps(payload, ensure_ascii=False, indent=4, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _render_watched_folders(paths: Iterable[str]) -> bytes:
    """Render qBittorrent's current ``watched_folders.json`` schema.

    Tixati's observed profile does not contain category associations for
    watched folders. The generated watcher entries therefore use qBittorrent's
    normal automatic-management defaults and leave category/tags empty.
    """

    payload: dict[str, dict[str, object]] = {}
    for path in paths:
        payload[path] = {
            "add_torrent_params": {
                "category": "",
                "content_layout": "Original",
                "download_limit": -1,
                "download_path": "",
                "operating_mode": "AutoManaged",
                "ratio_limit": -2,
                "save_path": "",
                "seeding_time_limit": -2,
                "skip_checking": False,
                "stopped": False,
                "tags": [],
                "upload_limit": -1,
                "use_auto_tmm": True,
            },
            "recursive": False,
        }
    return (json.dumps(payload, ensure_ascii=False, indent=4, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _render_migration_metadata(
    *,
    source_dir: Path,
    categories: Iterable[CoreCategory] | None,
    settings: TixatiSettings | None,
    watched_folders: Iterable[str],
) -> bytes | None:
    """Render source facts that qBittorrent cannot represent directly.

    This is intentionally JSON rather than another private binary format. It
    gives a human or future importer a loss-minimizing record of decisions
    made during migration without copying credentials from the source proxy
    registry or dumping opaque multi-megabyte databases.
    """

    payload: dict[str, object] = {
        "format": 1,
        "source": str(source_dir),
    }
    category_values: dict[str, dict[str, object]] = {}
    if categories is not None:
        for category in categories:
            item: dict[str, object] = {}
            for key, value in (
                ("save_path", category.save_path),
                ("secondary_path", category.secondary_path),
                ("seed_count_stop", category.seed_count_stop),
                ("seeding_ratio_stop", category.seeding_ratio_stop),
                ("seeding_minutes_stop", category.seeding_minutes_stop),
            ):
                if value is not None:
                    item[key] = value
            category_values[category.name] = item
    if category_values:
        payload["categories"] = category_values
    watched = tuple(watched_folders)
    if watched:
        payload["watched_folders"] = list(watched)

    if settings is not None:
        setting_values: dict[str, object] = {}
        for source_name, output_name in (
            ("readthrottle_mode", "read_throttle_mode"),
            ("readthrottle_rate", "read_throttle_rate"),
            ("writethrottle_mode", "write_throttle_mode"),
            ("writethrottle_rate", "write_throttle_rate"),
        ):
            value = settings.get(source_name)
            if isinstance(value, (int, float)):
                setting_values[output_name] = value
        quotas = settings.get_entry("bwquotas")
        if quotas is not None and isinstance(quotas.value, bytes):
            setting_values["bandwidth_quotas_hex"] = quotas.value.hex()
        if setting_values:
            payload["unmapped_settings"] = setting_values
        opaque_files = [
            name
            for name in ("bwpresets2.dat", "autothrottle2.dat", "scheduler2.dat")
            if (source_dir / name).is_file()
        ]
        if opaque_files:
            payload["opaque_settings_files"] = {
                "not_decoded": opaque_files,
                "reason": "these files use per-file runtime keys not yet validated",
            }

    if len(payload) == 2:
        return None
    return (json.dumps(payload, ensure_ascii=False, indent=4, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _prepare_specs(
    specs: list[TransferSpec],
    *,
    now: int,
    progress: Progress | None = None,
) -> list[_Prepared]:
    return _prepare_pairs(
        ((_load_torrent(spec.torrent_path), spec) for spec in specs),
        now=now,
        progress=progress,
    )


def _prepare_pairs(
    pairs: Iterable[tuple[_Torrent, TransferSpec]],
    *,
    now: int,
    progress: Progress | None = None,
) -> list[_Prepared]:
    # Preparation is kept separate from writing so all output paths can be
    # checked for conflicts before the first file is replaced.
    prepared: list[_Prepared] = []
    seen: dict[str, Path] = {}
    for index, (torrent, spec) in enumerate(pairs, start=1):
        if progress is not None and (index == 1 or index % 250 == 0):
            progress(f"preparing resume data {index}")
        info_hash_hex = torrent.info_hash.hex()
        if info_hash_hex in seen:
            raise MigrationError(
                f"duplicate info hash {info_hash_hex} in {seen[info_hash_hex]} "
                f"and {spec.torrent_path}"
            )
        seen[info_hash_hex] = spec.torrent_path
        prepared.append(
            _Prepared(
                torrent=torrent,
                spec=spec,
                fastresume=_make_fastresume(torrent, spec, now=now),
            )
        )
    return prepared


def migrate(
    tixati_dir: str | Path,
    output_dir: str | Path,
    *,
    torrent_source: str | Path | None = None,
    manifest: str | Path | None = None,
    save_path: str | None = None,
    category: str = "",
    tags: Iterable[str] = (),
    paused: bool = True,
    content_layout: str = "Original",
    path_maps: Iterable[tuple[str, str]] = (),
    overwrite: bool = False,
    write_settings: bool = False,
    settings_path: str | Path | None = None,
    incomplete_path: str = "",
    now: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Populate a qBittorrent profile directory from torrent metadata.

    ``output_dir`` is the profile root.  Generated transfer files always go
    under its ``BT_backup`` child; default settings go directly in the profile
    root beside that directory.

    The Tixati directory is inspected for context and for recursively stored
    ``.torrent`` files.  When no explicit ``torrent_source`` or manifest is
    supplied, a version-2 ``core2.dat`` is reverse-engineered directly.  The
    converter reconstructs the embedded bencoded torrent info dictionaries;
    it imports only private state whose serialized shape has been validated,
    including the aggregate piece bitmap and category download location.
    """

    source_dir = Path(tixati_dir).expanduser().resolve()
    profile_dir = Path(output_dir).expanduser().resolve()
    destination = profile_dir / "BT_backup"
    if not source_dir.is_dir():
        raise MigrationError(f"Tixati source is not a directory: {source_dir}")
    path_map_tuple = tuple(path_maps)
    tag_tuple = tuple(tags)
    source_settings: TixatiSettings | None = None
    watched_folders: tuple[str, ...] = ()
    if write_settings:
        settings_file = source_dir / "settings2.dat"
        if settings_file.is_file():
            try:
                source_settings = read_settings(settings_file, progress=progress)
            except (OSError, SettingsFormatError) as exc:
                raise MigrationError(
                    f"cannot reverse-engineer {settings_file}: {exc}"
                ) from exc
            _notify(progress, f"decoded {len(source_settings.entries)} Tixati settings")
            watched_folders = tuple(
                _apply_path_maps(path, path_map_tuple)
                for path in source_settings.watched_folder_paths()
            )
            if watched_folders:
                _notify(progress, f"found {len(watched_folders)} watched folders")

    skipped_info_hashes: tuple[str, ...] = ()
    direct_core_extraction: CoreExtraction | None = None
    core_pairs: list[tuple[_Torrent, TransferSpec]] | None = None
    core_categories: tuple[CoreCategory, ...] | None = None
    # Input precedence is explicit: manifests override everything, an explicit
    # torrent source is used as requested, and a local core2.dat is the direct
    # fallback when neither was supplied.
    if manifest is not None:
        manifest_path = Path(manifest).expanduser().resolve()
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"cannot read manifest {manifest_path}: {exc}") from exc
        specs = _manifest_specs(
            manifest_path,
            data,
            cli_save_path=save_path,
            cli_category=category,
            cli_tags=tag_tuple,
            cli_paused=paused,
            cli_content_layout=content_layout,
            path_maps=path_map_tuple,
        )
    elif torrent_source is None and (source_dir / "core2.dat").is_file():
        core_path = source_dir / "core2.dat"
        try:
            # Very small placeholder files are useful in tests and should fall
            # through to the normal exported-torrent error below.
            if core_path.stat().st_size >= 24:
                direct_core_extraction = extract_core_file(
                    core_path,
                    progress=progress,
                )
        except (OSError, CoreFormatError) as exc:
            raise MigrationError(f"cannot reverse-engineer {core_path}: {exc}") from exc
        if direct_core_extraction is not None and direct_core_extraction.transfers:
            skipped_info_hashes = tuple(
                value.hex() for value in direct_core_extraction.skipped_info_hashes
            )
            core_categories = direct_core_extraction.categories
            category_by_name = {
                definition.name: definition
                for definition in core_categories
            }
            core_pairs = []
            for index, item in enumerate(direct_core_extraction.transfers, start=1):
                if progress is not None and (index == 1 or index % 250 == 0):
                    progress(f"building torrent files {index}/{len(direct_core_extraction.transfers)}")
                torrent = _core_torrent(item)
                transfer_category = category or item.category or ""
                category_definition = category_by_name.get(transfer_category)
                complete = item.piece_state is not None and all(item.piece_state)
                ratio_limit = -2000
                seeding_time_limit = -2
                if category_definition is not None:
                    if (
                        category_definition.seeding_ratio_stop is not None
                        and category_definition.seeding_ratio_stop > 0
                    ):
                        # qBittorrent stores per-torrent ratio limits in its
                        # integer thousandths representation; -2000 means
                        # “use the global limit”.
                        ratio_limit = round(
                            category_definition.seeding_ratio_stop * 1000
                        )
                    if (
                        category_definition.seeding_minutes_stop is not None
                        and category_definition.seeding_minutes_stop > 0
                    ):
                        seeding_time_limit = category_definition.seeding_minutes_stop
                core_pairs.append(
                    (
                        torrent,
                        TransferSpec(
                            torrent_path=torrent.source_path,
                            save_path=_apply_path_maps(
                                save_path
                                if save_path is not None
                                else (item.category_path or ""),
                                path_map_tuple,
                            ),
                            # An explicit CLI category is an override.  When
                            # it is empty, retain the category found in
                            # Tixati's per-transfer generic settings map.
                            category=transfer_category,
                            # A CLI tag list is an explicit override; an
                            # absent override preserves tags decoded from the
                            # transfer's typed Tixati settings map.
                            tags=tag_tuple or item.tags,
                            paused=paused,
                            content_layout=content_layout,
                            added_time=item.created_time,
                            completed_time=(
                                item.last_activity_time if complete else None
                            ),
                            last_download=(
                                item.last_activity_time
                                if item.last_activity_time is not None
                                else None
                            ),
                            last_seen_complete=(
                                item.last_activity_time
                                if complete and item.last_activity_time is not None
                                else None
                            ),
                            ratio_limit=ratio_limit,
                            seeding_time_limit=seeding_time_limit,
                            total_downloaded=item.total_downloaded,
                            total_uploaded=item.total_uploaded,
                            piece_state=item.piece_state,
                        ),
                    )
                )
            direct_core_extraction = None
        else:
            specs = _default_specs(
                source_dir,
                save_path=save_path,
                category=category,
                tags=tag_tuple,
                paused=paused,
                content_layout=content_layout,
                path_maps=path_map_tuple,
            )
    else:
        source = (
            Path(torrent_source).expanduser().resolve()
            if torrent_source is not None
            else source_dir
        )
        if not source.is_dir():
            raise MigrationError(f"torrent source is not a directory: {source}")
        specs = _default_specs(
            source,
            save_path=save_path,
            category=category,
            tags=tag_tuple,
            paused=paused,
            content_layout=content_layout,
            path_maps=path_map_tuple,
        )

    timestamp = int(time.time()) if now is None else now
    if content_layout not in {"Original", "Subfolder"}:
        raise MigrationError("content_layout must be Original or Subfolder")
    _notify(progress, "preparing qBittorrent resume data")
    prepared = (
        _prepare_pairs(core_pairs, now=timestamp, progress=progress)
        if core_pairs is not None
        else _prepare_specs(specs, now=timestamp, progress=progress)
    )
    _notify(progress, f"prepared {len(prepared)} transfer entries")
    categories = _category_specs(
        prepared,
        core_categories=core_categories,
        cli_save_path=save_path,
        path_maps=path_map_tuple,
    )
    if categories:
        _notify(progress, f"prepared {len(categories)} category definitions")
    watched_folders_path = profile_dir / "watched_folders.json" if watched_folders else None
    metadata_bytes = _render_migration_metadata(
        source_dir=source_dir,
        categories=core_categories,
        settings=source_settings,
        watched_folders=watched_folders,
    )
    metadata_path = profile_dir / "tixati-migration.json" if metadata_bytes else None
    if metadata_path is not None:
        _notify(progress, f"prepared migration metadata {metadata_path}")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(f"cannot create output directory {destination}: {exc}") from exc

    planned_paths: list[Path] = []
    _notify(progress, f"writing {len(prepared)} torrent/resume pairs")
    for index, item in enumerate(prepared, start=1):
        if progress is not None and (index == 1 or index % 250 == 0 or index == len(prepared)):
            progress(f"writing output {index}/{len(prepared)}")
        stem = item.torrent.info_hash.hex()
        planned_paths.extend(
            (destination / f"{stem}.torrent", destination / f"{stem}.fastresume")
        )
    if write_settings:
        planned_paths.append(
            Path(settings_path).expanduser().resolve()
            if settings_path is not None
            else profile_dir / "qBittorrent.conf"
        )
    categories_path = profile_dir / "categories.json" if categories else None
    if categories_path is not None:
        planned_paths.append(categories_path)
    if watched_folders_path is not None:
        planned_paths.append(watched_folders_path)
    if metadata_path is not None:
        planned_paths.append(metadata_path)
    if not overwrite:
        conflicts = [path for path in planned_paths if path.exists()]
        if conflicts:
            listed = ", ".join(str(path) for path in conflicts[:3])
            suffix = "..." if len(conflicts) > 3 else ""
            raise MigrationError(f"output contains existing files: {listed}{suffix}")

    for item in prepared:
        stem = item.torrent.info_hash.hex()
        output_mtime = (
            item.spec.added_time
            if item.spec.added_time is not None
            else timestamp
        )
        _atomic_write(
            destination / f"{stem}.torrent",
            item.torrent.raw,
            overwrite=overwrite,
            mtime=output_mtime,
        )
        _atomic_write(
            destination / f"{stem}.fastresume",
            item.fastresume,
            overwrite=overwrite,
            mtime=output_mtime,
        )

    generated_settings: Path | None = None
    if write_settings:
        generated_settings = (
            Path(settings_path).expanduser().resolve()
            if settings_path is not None
            else profile_dir / "qBittorrent.conf"
        )
        # Apply the same path rewrite to the global default that is applied to
        # each resume record.  Without this, a migrated profile could have
        # resume files pointing at the new machine while qBittorrent's global
        # default still pointed at Tixati's old absolute path.
        settings_save_path = _apply_path_maps(save_path or "", path_map_tuple)
        if not settings_save_path and source_settings is not None:
            settings_save_path = _apply_path_maps(
                source_settings.path("incpath") or "", path_map_tuple
            )
        if generated_settings.exists() and not overwrite:
            raise MigrationError(f"refusing to overwrite existing file {generated_settings}")
        generated_settings.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            generated_settings,
            render_qbittorrent_conf(
                save_path=settings_save_path,
                incomplete_path=incomplete_path,
                tixati_settings=source_settings,
            ).encode("utf-8"),
            overwrite=overwrite,
        )
        _notify(progress, f"wrote settings {generated_settings}")

    generated_categories: Path | None = None
    if categories_path is not None:
        generated_categories = categories_path
        _atomic_write(
            generated_categories,
            _render_categories(categories),
            overwrite=overwrite,
        )
        _notify(progress, f"wrote categories {generated_categories}")

    generated_watched_folders: Path | None = None
    if watched_folders_path is not None:
        generated_watched_folders = watched_folders_path
        _atomic_write(
            generated_watched_folders,
            _render_watched_folders(watched_folders),
            overwrite=overwrite,
        )
        _notify(progress, f"wrote watched folders {generated_watched_folders}")

    generated_metadata: Path | None = None
    if metadata_path is not None and metadata_bytes is not None:
        generated_metadata = metadata_path
        _atomic_write(
            generated_metadata,
            metadata_bytes,
            overwrite=overwrite,
        )
        _notify(progress, f"wrote migration metadata {generated_metadata}")

    return MigrationReport(
        torrent_count=len(prepared),
        fastresume_count=len(prepared),
        output_dir=profile_dir,
        settings_path=generated_settings,
        categories_path=generated_categories,
        watched_folders_path=generated_watched_folders,
        metadata_path=generated_metadata,
        skipped_count=len(skipped_info_hashes),
        skipped_info_hashes=skipped_info_hashes,
    )
