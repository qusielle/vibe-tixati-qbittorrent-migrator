# Handoff

This document is for a new session taking over work on the project.

## Project

Repository:

```text
/home/kasumi/devel/codex_sandbox/tixati-qbittorrent-migrator
```

Source profile used during investigation:

```text
/home/kasumi/devel/codex_sandbox/tixati
```

The source profile was investigated statically. Tixati was not run against
the real profile and no network access was allowed while reverse-engineering
it. A copied disposable profile was used for limited dynamic investigation
under network isolation. Large-profile commands must continue to use:

```bash
ulimit -v 20971520
```

The repository is clean at handoff.

## Current commits

The requested work is split into atomic commits:

```text
18ca506 Preserve tags watched folders and source metadata
f5f2ab3 Import Tixati network and allocation settings
dbd12d6 Migrate activity timestamps and category limits
427955e Remove redundant listen port override
25d161c Import compatible Tixati global settings
3bf8e0c Reverse-engineer Tixati settings2 format
```

Do not rewrite or squash these commits unless explicitly requested.

## How to test

`uv` was not available in the container during the last work session. Tests
were run with the prepared environment:

```bash
/tmp/tqb-test-venv/bin/python -m pytest -q
python3 -m compileall -q src tests
```

The final result was 16 passing tests. The project also contains `pyproject.toml`
and `uv.lock`; a future session can use `uv venv && uv sync --locked --dev`
when `uv` is available.

## Normal invocation

The checkout supports module execution without installing the package:

```bash
tixati-qbittorrent-migrator/.venv/bin/python \
  -m tixati-qbittorrent-migrator \
  /path/to/tixati \
  /path/to/qbittorrent/profile \
  --write-settings
```

The output argument is the qBittorrent profile root. The converter creates
`output_dir/BT_backup`; do not pass `BT_backup` as the output root. The old
`--listen-port` option was intentionally removed. The incoming port is
imported from Tixati settings when `--write-settings` is used.

## Current functionality

### `core2.dat`

The reader authenticates and decrypts Tixati v2 files using the observed
RC4-compatible stream and SHA-1 envelope. It reconstructs exact bencoded
`info` bytes from Tixati's split metadata fields and verifies the SHA-1 info
hash before writing a torrent.

The recognized transfer fields include:

- metadata path, trackers, comments, creator, and torrent creation date;
- Tixati Created/Added timestamp `0x1103f`;
- later activity/state timestamp `0x11040`;
- all-time downloaded/uploaded counters and payload/overhead components;
- validated aggregate piece bitmap `0x1400a`;
- transfer category and candidate typed tag lists.

Direct migration writes qBittorrent `.torrent` and `.fastresume` pairs,
preserves raw info bytes, imports validated piece state, counters, category,
tags, and paths, and sets both output file mtimes to `added_time`.

For complete transfers with a validated piece bitmap, `0x11040` is used as
qBittorrent completion time, last-download time, and last-seen-complete time.
For incomplete transfers it is used only as last activity/download time.

### Categories and automation

User categories (`cattype = 0`) are written to `categories.json`. Their
`dlloc` path becomes the default per-torrent save path unless overridden by
`--save-path`; `--path-map` rewrites paths for a new machine.

Category ratio and positive seeding-minute limits are converted to per-torrent
qBittorrent resume limits. Category `sdloc` and seed-count stopping are kept
in `tixati-migration.json` because they have no safe direct qBittorrent
equivalent.

### `settings2.dat`

`settings2.dat` uses a separate observed key and the same version/encrypted
body/SHA-1 envelope. The reader handles named values, numeric legacy fields,
type-12 paths, and nested type-12-list/type-11-map registries.

When `--write-settings` is enabled, the generated `qBittorrent.conf` imports
compatible values including:

- incoming port and default save path;
- UPnP/port-forwarding state and DHT mode;
- connection/upload/managed-active limits;
- peer encryption policy;
- TCP/uTP/Both protocol preference;
- full/fast/sparse allocation as qBittorrent preallocation;
- peer proxy (`pproxy...`) as qBittorrent's BitTorrent proxy;
- local interface binding (`local_ip`, including observed `tun0`).

The real source settings observed during investigation included:

```text
incport=15363
incpath=/home/kasumi/Downloads/t
peercryptmode=3
incconncryptmode=2
peerconnproto=3
filealloctype=2
local_ip=tun0
pproxymode=3
pproxyaddress=127.0.0.1
pproxyport=12345
readthrottle_rate=512000
writethrottle_rate=100000
```

### Output metadata

`tixati-migration.json` preserves source-only information without copying
proxy credentials. It can contain category secondary paths and automation,
disk throttle values, authenticated `bwquotas` bytes as hex, and a list of
opaque auxiliary settings files.

If named watched-folder paths are found, the converter writes
`watched_folders.json` using qBittorrent's current watcher schema. No such
path was present in the supplied profile.

## Real-profile validation already completed

Using the real profile, with the 20 GiB virtual-memory cap:

```text
core2.dat: 220,334,243 bytes
reconstructable transfers: 3,253
skipped hash-only/link records: 10
Created timestamps recovered: 3,253
activity timestamps recovered: 3,217
validated complete transfers: 2,937
user categories: 11
decoded transfer tags: 0
watched-folder paths: none
```

A disposable end-to-end output was written under `/tmp`; it contained 3,253
torrent files, 3,253 fastresume files, `categories.json`,
`qBittorrent.conf`, and `tixati-migration.json`.

## Known limitations / next investigations

These are intentionally not fabricated:

- Tixati active-time duration was not identified in the decoded transfer
  fields.
- A separate last-upload timestamp was not identified. `last_upload` remains
  zero.
- Tixati disk read/write throttles have no qBittorrent equivalent; they are
  preserved as source metadata rather than confused with disk-cache modes.
- `bwquotas` is decoded only as authenticated opaque bytes. The quota maps
  use numeric keys and values whose semantics were not sufficiently proven.
- `bwpresets2.dat`, `autothrottle2.dat`, and `scheduler2.dat` use per-file
  runtime keys that have not been fully decoded.
- Only the peer proxy scope is mapped. Tracker, DHT-UDP, upload, and other
  Tixati proxy scopes are not merged into qBittorrent's single proxy setting.
- Tixati proxy SSL mode is not represented.
- Seed-count automation and category `sdloc` have no safe direct mapping.
- The supplied profile contained no persisted tags or watched-folder paths;
  the code supports only narrowly typed candidate values to avoid false
  positives.
- Per-file Tixati priorities/status/date fields remain unimplemented.

If continuing reverse-engineering, use copied profiles only, preserve the
20 GiB memory cap, and add a focused fixture plus an atomic commit for each
new proven field mapping. Do not launch Tixati against the real source
profile.

## Important files

- `src/tixati_qbittorrent/tixati_core.py` — encrypted core reader and transfer
  reconstruction.
- `src/tixati_qbittorrent/tixati_settings.py` — settings envelope and typed
  registry parser.
- `src/tixati_qbittorrent/config.py` — qBittorrent settings mappings.
- `src/tixati_qbittorrent/migration.py` — normalization, resume generation,
  conflict checks, and atomic writes.
- `src/tixati_qbittorrent/model.py` — public transfer/category/report models.
- `tests/test_core.py` — core and direct-migration fixtures.
- `tests/test_settings.py` — settings and nested proxy fixtures.
- `tests/test_migration.py` — output layout, path mapping, and watched-folder
  tests.
- `README.md` — user-facing format and usage documentation.
- `ARCHITECTURE.md` — contributor-oriented design notes.
