# Tixati → qBittorrent migrator

This package creates qBittorrent `BT_backup` entries from `.torrent` metadata exported from a Tixati profile. For each input torrent it writes:

```text
<v1-info-hash>.torrent
<v1-info-hash>.fastresume
```

The generated resume data is deliberately conservative: unless a manifest supplies a piece state, every piece is marked unchecked and the torrent is paused. qBittorrent will recheck the existing files before downloading or seeding. This avoids claiming data that was not verified by the converter.

## Important limitation of the supplied profile

Tixati stores its transfer list and metadata in proprietary binary files, especially `core2.dat`. This project does not reverse-engineer that private format. The supplied Tixati directory contains `core2.dat` and settings files, but no `.torrent` files, so it is not sufficient by itself to produce qBittorrent entries.

Before running the converter, stop Tixati and export the metadata. In Tixati's Transfers view, select the transfers, then use **Share → Save .torrent** and save them to a staging directory. Tixati also has an Import/Export facility, but the converter needs the actual `.torrent` metadata to construct qBittorrent's hash-named files.

## Install

The runtime has no third-party dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

For development tests:

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Basic migration

The command recursively discovers `.torrent` files under the source directory. Use a separate torrent staging directory when the Tixati config and exported metadata are separate:

```bash
tixati-to-qbittorrent \
  /path/to/tixati \
  /path/to/qBittorrent/profile/BT_backup \
  --torrent-source /path/to/exported-torrents \
  --save-path /path/to/downloads
```

The default output is stopped/paused. qBittorrent should be closed while its profile is being populated. Back up any existing `BT_backup` directory first. The converter never overwrites existing files unless `--overwrite` is supplied, and individual files are replaced atomically.

If the paths on the old machine differ from the new machine, rewrite them with a prefix map:

```bash
--path-map 'C:\\Downloads=/srv/downloads'
```

Repeat `--path-map` for more prefixes. If all transfers share one save path, `--save-path` is enough.

## Per-torrent manifest

Use a JSON manifest when transfers have different save paths, categories, tags, file priorities, or known piece states. Paths are relative to the manifest file unless absolute:

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

`piece_state` must contain one `0` or `1` per piece. Omit it unless the state came from a trusted export; omission makes the converter emit all-zero state and forces a recheck. `file_priority` contains one qBittorrent priority value per torrent file.

Run it with:

```bash
tixati-to-qbittorrent /path/to/tixati /path/to/profile/BT_backup \
  --manifest /path/to/manifest.json
```

The current implementation accepts v1-compatible torrents, including v1/v2 hybrid torrents. v2-only torrents are reported as unsupported because qBittorrent's v1 hash-named resume layout cannot be generated from them by this version.

## Optional qBittorrent settings

Add `--write-settings` to write a minimal `qBittorrent.conf` next to `BT_backup` (or use `--settings-path`). It sets the default save path, optional incomplete path, and optional listening port. Tixati's unrelated settings database is not mechanically translated.

## Library use

```python
from tixati_qbittorrent import migrate

report = migrate(
    "/path/to/tixati",
    "/path/to/qBittorrent/profile/BT_backup",
    torrent_source="/path/to/exported-torrents",
    save_path="/srv/downloads",
)
print(report.torrent_count)
```
