# Tixati → qBittorrent migrator

This module reverse-engineers Tixati v2 `core2.dat` and creates qBittorrent
`BT_backup` entries. For each reconstructable transfer it writes:

```text
<info-hash>.torrent
<info-hash>.fastresume
```

The converter also retains tracker URLs found in the Tixati transfer records.
The generated resume data is conservative: every piece is marked unchecked and
the transfer is paused by default, so qBittorrent will recheck the existing
files before downloading or seeding.

## Reverse-engineered format

Tixati v2 core files are not ZIP, bencode, or a normal database. Their version-2
payload is an RC4-compatible stream encrypted with a fixed 24-byte reader key
and authenticated by a SHA-1 trailer. The decrypted payload is a custom field
stream. Transfer metadata stores the bencoded `info` dictionary as:

1. a bencoded prefix ending at `pieces<N>:`;
2. a separate `N`-byte piece-hash field; and
3. a bencoded suffix.

The parser recombines those fields and verifies the result against Tixati's
stored 20-byte info hash before emitting a torrent. It ignores unknown private
fields rather than guessing their meaning. It does not claim Tixati's piece
completion state, download location, category names, or other UI state that is
not needed to create valid qBittorrent metadata.

On the supplied profile, 3,253 transfers had complete torrent metadata and 10
additional records were hash-only/link entries without piece metadata. Those
10 are reported and skipped; no fabricated torrent is produced for them.

## Install

The only runtime dependency is PyCryptodome, used for fast RC4 processing of
large profiles. A pure-Python fallback is included but is much slower.

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

For development tests:

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Direct migration from a Tixati profile

Stop Tixati and work from a backup of its profile. The converter does not start
Tixati or make network connections:

```bash
tixati-to-qbittorrent \
  /path/to/tixati \
  /path/to/qBittorrent/profile/BT_backup \
  --save-path /srv/downloads
```

With no `--torrent-source` and no manifest, a version-2 `core2.dat` in the
input directory is parsed directly. The default output is stopped/paused for a
safe recheck. qBittorrent should be closed while its profile is being
populated. Back up any existing `BT_backup` directory first. The converter
never overwrites existing files unless `--overwrite` is supplied, and each
file is replaced atomically.

If the old and new machines use different paths, rewrite a prefix:

```bash
--path-map 'C:\\Downloads=/srv/downloads'
```

Repeat `--path-map` for additional prefixes. `--save-path` is enough when all
transfers share one destination. Use `--write-settings` to also create a
minimal `qBittorrent.conf` beside `BT_backup`.

## Exported `.torrent` mode

An explicit `--torrent-source` keeps the original `.torrent` files and is
useful when a Tixati profile is unavailable or when you want to control the
input set manually:

```bash
tixati-to-qbittorrent \
  /path/to/tixati \
  /path/to/qBittorrent/profile/BT_backup \
  --torrent-source /path/to/exported-torrents \
  --save-path /srv/downloads
```

Tixati's documented **Share → Save .torrent** action is an alternative source
of metadata. The converter accepts v1-compatible torrents, including v1/v2
hybrids. v2-only torrents are currently rejected because this qBittorrent
resume layout is keyed by the v1 info hash.

## Per-torrent manifest

Use a JSON manifest for different save paths, categories, tags, priorities, or
trusted piece states. Paths are relative to the manifest unless absolute:

```json
{
  "defaults": {
    "save_path": "/srv/downloads",
    "category": "from-tixati",
    "paused": true
  },
  "torrents": [
    {
      "torrent": "exported/0123456789abcdef0123456789abcdef01234567.torrent",
      "save_path": "/srv/downloads/music",
      "tags": ["migrated"],
      "file_priority": [1, 1, 0],
      "piece_state": [1, 0, 1, 1]
    }
  ]
}
```

`piece_state` must contain one `0` or `1` per piece. Omit it unless it came
from a trusted source; omission emits all-zero state and forces a recheck.
`file_priority` contains one qBittorrent priority value per torrent file.

```bash
tixati-to-qbittorrent /path/to/tixati /path/to/profile/BT_backup \
  --manifest /path/to/manifest.json
```

## Library use

```python
from tixati_qbittorrent import migrate

report = migrate(
    "/path/to/tixati",
    "/path/to/qBittorrent/profile/BT_backup",
    save_path="/srv/downloads",
    write_settings=True,
)
print(report.torrent_count, report.skipped_count)
```
