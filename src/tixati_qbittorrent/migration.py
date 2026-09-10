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

from .bencode import BValue, BencodeError, decode, encode
from .config import render_qbittorrent_conf
from .model import MigrationReport, TransferSpec
from .tixati_core import (
    CoreExtraction,
    CoreFormatError,
    CoreTransfer,
    Progress,
    extract_core_file,
)


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

    info_hash = (
        expected_info_hash
        if expected_info_hash is not None
        else hashlib.sha1(encode(info)).digest()
    )
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

    resume: dict[bytes, BValue] = {
        b"active_time": 0,
        b"added_time": added_time,
        b"allocation": b"sparse",
        b"apply_ip_filter": 1,
        b"auto_managed": 0 if spec.paused else 1,
        b"completed_time": now if complete else 0,
        b"disable_dht": 1 if private else 0,
        b"disable_lsd": 1 if private else 0,
        b"disable_pex": 1 if private else 0,
        b"download_rate_limit": -1,
        b"file-format": b"libtorrent resume file",
        b"file-version": 1,
        b"file_priority": file_priority,
        b"finished_time": 0,
        b"httpseeds": [],
        b"info-hash": torrent.info_hash,
        b"last_download": 0,
        b"last_seen_complete": now if complete else 0,
        b"last_upload": 0,
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
        b"qBt-ratioLimit": -2000,
        b"qBt-savePath": save_path.encode("utf-8"),
        b"qBt-seedStatus": 1 if complete else 0,
        b"qBt-seedingTimeLimit": -2,
        b"qBt-stopCondition": b"None",
        b"qBt-tags": [tag.encode("utf-8") for tag in spec.tags],
        b"save_path": save_path.encode("utf-8"),
        b"seed_mode": 0,
        b"seeding_time": 0,
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
    listen_port: int | None = None,
    now: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Convert torrent metadata into a qBittorrent ``BT_backup`` directory.

    The Tixati directory is inspected for context and for recursively stored
    ``.torrent`` files.  When no explicit ``torrent_source`` or manifest is
    supplied, a version-2 ``core2.dat`` is reverse-engineered directly.  The
    converter reconstructs the embedded bencoded torrent info dictionaries;
    it does not attempt to guess Tixati's private piece-completion state.
    """

    source_dir = Path(tixati_dir).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise MigrationError(f"Tixati source is not a directory: {source_dir}")
    path_map_tuple = tuple(path_maps)
    tag_tuple = tuple(tags)

    skipped_info_hashes: tuple[str, ...] = ()
    direct_core_extraction: CoreExtraction | None = None
    core_pairs: list[tuple[_Torrent, TransferSpec]] | None = None
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
            core_pairs = []
            for index, item in enumerate(direct_core_extraction.transfers, start=1):
                if progress is not None and (index == 1 or index % 250 == 0):
                    progress(f"building torrent files {index}/{len(direct_core_extraction.transfers)}")
                torrent = _core_torrent(item)
                core_pairs.append(
                    (
                        torrent,
                        TransferSpec(
                            torrent_path=torrent.source_path,
                            save_path=_apply_path_maps(save_path or "", path_map_tuple),
                            # An explicit CLI category is an override.  When
                            # it is empty, retain the category found in
                            # Tixati's per-transfer generic settings map.
                            category=category or item.category or "",
                            tags=tag_tuple,
                            paused=paused,
                            content_layout=content_layout,
                            added_time=item.created_time,
                            total_downloaded=item.total_downloaded,
                            total_uploaded=item.total_uploaded,
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
            else destination.parent / "qBittorrent.conf"
        )
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
            else destination.parent / "qBittorrent.conf"
        )
        if generated_settings.exists() and not overwrite:
            raise MigrationError(f"refusing to overwrite existing file {generated_settings}")
        generated_settings.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            generated_settings,
            render_qbittorrent_conf(
                save_path=save_path or "",
                incomplete_path=incomplete_path,
                listen_port=listen_port,
            ).encode("utf-8"),
            overwrite=overwrite,
        )
        _notify(progress, f"wrote settings {generated_settings}")

    return MigrationReport(
        torrent_count=len(prepared),
        fastresume_count=len(prepared),
        output_dir=destination,
        settings_path=generated_settings,
        skipped_count=len(skipped_info_hashes),
        skipped_info_hashes=skipped_info_hashes,
    )
