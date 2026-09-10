# Tixati → qBittorrent Migrator

A small, dependency-light Python tool for reconstructing qBittorrent
`BT_backup` entries from Tixati transfer metadata.

For each reconstructable transfer, it writes:

```text
<info-hash>.torrent
<info-hash>.fastresume
```

Tracker URLs are retained when present. Generated resume data is deliberately
conservative: transfers are paused and all pieces are marked unchecked, so
qBittorrent verifies existing data before downloading or seeding.

## Features

- Reads Tixati v2 `core2.dat` directly; no `.torrent` export is required.
- Reconstructs the original bencoded torrent `info` bytes and verifies their
  SHA-1 info hash before writing anything.
- Preserves tracker URLs and torrent-level creation metadata found in transfer
  records.
- Carries over Tixati per-transfer categories to qBittorrent categories.
- Uses a Tixati category's download location as the default save path when no
  explicit save path is supplied.
- Sets each generated `.torrent` and `.fastresume` mtime to its
  qBittorrent `added_time`.
- Supports exported `.torrent` files and JSON manifests as alternate inputs.
- Emits atomic output files and an optional minimal `qBittorrent.conf`.
- Provides a checkout launcher, console script, and importable Python API.

## Reverse-engineered `core2.dat` format

`core2.dat` is a custom binary container rather than a ZIP archive, bencoded
database, or standard SQLite file. The implementation currently understands
version 2 of the container.

### Outer container

At the file level, the layout is:

```text
uint32-le version       = 2
bytes   encrypted_body
bytes   sha1_digest      = SHA1(decrypted_body), exactly 20 bytes
```

The body is encrypted with an RC4-compatible stream whose reader key is
embedded in the Tixati binary. The digest authenticates the decrypted body;
files with an invalid version or digest are rejected before parsing.

### Serialized fields

The decrypted body is a stream of private field records. A recognized record
has this shape:

```text
05 00 00 00 04        field preamble
<field-id:u32-le>
<length:u32-le>       includes the following type byte
<type:u8>
<payload:length-1>
```

For example, an info-hash field with ID `0x11009` and a 20-byte payload starts
like this:

```text
05 00 00 00 04
09 10 01 00           # 0x11009, little-endian
15 00 00 00           # 21 bytes: one type byte + 20 payload bytes
02                    # byte-string type
<20 raw SHA-1 bytes>
```

Important transfer fields include:

| Field | Meaning |
| --- | --- |
| `0x11003` | raw piece-hash bytes |
| `0x11006` | torrent metadata creation date, type 6 Unix seconds |
| `0x11007` | torrent comment, type 2 byte string |
| `0x11008` | torrent creator, type 2 byte string |
| `0x11009` | torrent v1 info hash |
| `0x1101e` | original metadata path or a hash-link label |
| `0x11027` | bencoded info-dictionary prefix |
| `0x11028` | bencoded info-dictionary suffix |
| `0x1103f` | per-transfer Created/Added timestamp, type 6 `uint64` Unix seconds |
| `0x12001` | tracker URL |
| `0x14040` | all-time downloaded bytes, including protocol overhead |
| `0x14041` | all-time uploaded bytes, including protocol overhead |
| `0x14042` | payload downloaded bytes |
| `0x14043` | payload uploaded bytes |
| `0x14044` | download protocol-overhead bytes |
| `0x14045` | upload protocol-overhead bytes |
| `0x1400a` | packed aggregate piece-completion bitmap |

Unknown fields are ignored. This is intentional: the file contains many
private fields for UI state, statistics, file metadata, and scheduler data.

The transfer-specific category is stored outside the numeric field stream in
a small generic settings map. In the observed format, an entry such as
`d_category` is encoded as a type-2 key followed by a type-2 value. The
reader scopes this lookup to the matching hash record and writes the value to
qBittorrent's `qBt-category` resume key. An explicit command-line category
still takes precedence.

### Reconstructing an `info` dictionary

Tixati splits the bencoded `pieces` value out of the surrounding dictionary.
For a simple single-file torrent, the relevant fields can be represented as:

```python
from hashlib import sha1

prefix = (
    b"d6:lengthi123e4:name11:example.bin"
    b"12:piece lengthi16384e6:pieces40:"
)
# piece_zero and piece_one are the two 16 KiB file chunks
piece_hashes = sha1(piece_zero).digest() + sha1(piece_one).digest()
suffix = b"e"

info_bytes = prefix + piece_hashes + suffix
info_hash = sha1(info_bytes).digest()
```

The resulting bencode is conceptually:

```text
d
  6:length       i123e
  4:name         11:example.bin
  12:piece length i16384e
  6:pieces       40:<raw piece hash 1><raw piece hash 2>
e
```

The actual field stream may place the piece-hash record before the metadata
fields. The parser associates candidates by piece length, reconstructs the
dictionary, decodes it, and checks `SHA1(info_bytes)` against the matching
`0x11009` field. Only hash-verified records are emitted.

The exact recovered `info_bytes` are retained when writing the `.torrent`
file. This matters because bencode dictionary key order is part of the hashed
byte sequence: decoding and re-encoding a non-canonical dictionary could
silently change its info hash.

When present, `0x11006`, `0x11007`, and `0x11008` are emitted as the standard
torrent root keys `creation date`, `comment`, and `created by`. They do not
affect the info hash because they are outside the `info` dictionary.

### Transfer counters

The six `0x14040`–`0x14045` counters occur in the same transfer record as the
metadata. Their structure is especially clear because the totals satisfy:

```text
0x14040 = 0x14042 + 0x14044   # downloaded payload + download overhead
0x14041 = 0x14043 + 0x14045   # uploaded payload + upload overhead
```

The two totals are copied to qBittorrent's `total_downloaded` and
`total_uploaded` resume keys. The four components are retained by the core
reader for callers that need the detailed accounting, while qBittorrent's
resume format has no separate destination for them.

The `0x1400a` payload begins with a little-endian piece count and then an
MSB-first bitmap. When its count and byte length match the reconstructed
torrent, the bitmap is expanded into qBittorrent's one-byte-per-piece
`pieces` state. An absent or malformed bitmap is treated conservatively as
unchecked pieces.

### Transfer dates

Tixati keeps the transfer's Created/Added date in the parent transfer record,
not in the bencoded torrent metadata. In observed version-2 profiles it is the
`0x1103f` field: a type-6 field with an eight-byte little-endian Unix timestamp.
For example, the payload below represents `2020-09-13 12:15:23 UTC`:

```text
05 00 00 00 04        field preamble
3f 10 01 00           field ID 0x1103f
09 00 00 00           9 bytes: one type byte plus 8 payload bytes
06                    unsigned 64-bit integer
5b 0d 5e 5f 00 00 00 00
```

The migrator copies that value to qBittorrent's `added_time` key in the
`.fastresume` file. The neighboring date-like fields occur later or are
missing in some records and behave like activity/completion bookkeeping, so
they are not used as the added date. This mapping is based on repeated field
behavior across real profiles rather than an official Tixati schema. When
using exported `.torrent` files without a matching core record, the source
does not contain this private date; the migrator then uses the migration time,
or a manifest-supplied `added_time`. Both generated files receive the same
Unix-second value as their filesystem modification time, which also preserves
the date in tools that inspect file metadata rather than bencode.

## Limitations

- Version-2 `core2.dat` is currently supported; newer formats may require new
  field mappings or encryption details.
- Tixati's aggregate piece-completion bitmap is converted when it passes the
  count and length checks described above. Per-file verification details and
  malformed/ambiguous bitmaps are not guessed.
- Direct `core2.dat` migrations preserve the Tixati Created/Added timestamp as
  qBittorrent's `added_time`. Standalone exported `.torrent` files have no such
  private field and therefore use the migration time unless the manifest
  supplies `added_time`.
- Tixati categories are copied when present. Download paths and other UI state
  are copied when their category definition supplies a location. Because that
  location may belong to another machine, use `--save-path` or `--path-map`
  when migrating between systems; a manifest remains available for per-torrent
  overrides.
- Hash-only/link records without embedded piece metadata are reported and
  skipped rather than turned into unverifiable torrents.
- v2-only `.torrent` files are rejected by the exported-metadata path because
  the generated qBittorrent layout is keyed by a v1 info hash.

## Installation

The runtime dependency is PyCryptodome, which makes RC4 processing of large
profiles fast. A pure-Python fallback is included, but is substantially
slower.

With uv:

```bash
uv venv
uv sync --locked --dev
```

This uses the Python version in `.python-version`, installs the project, and
installs development dependencies. Run tests with `uv run pytest`.

With standard venv/pip:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## Usage

### From a checkout

The repository includes a top-level launcher so it can be run without a build
or package installation. From the directory containing the checkout:

```bash
tixati-qbittorrent-migrator/.venv/bin/python \
  -m tixati-qbittorrent-migrator \
  /path/to/tixati \
  /path/to/qBittorrent/profile/BT_backup \
  --save-path /srv/downloads
```

The default output is paused. qBittorrent should be closed while its profile
is being populated, and existing `BT_backup` files are never overwritten
unless `--overwrite` is supplied.

### Installed package

After installation, these forms are equivalent:

```bash
tixati-to-qbittorrent /path/to/tixati /path/to/profile/BT_backup
python -m tixati_qbittorrent /path/to/tixati /path/to/profile/BT_backup
```

Use `--write-settings` to create a minimal `qBittorrent.conf` beside
`BT_backup`. If paths differ between systems, rewrite a prefix:

```bash
--path-map 'C:\\Downloads=/srv/downloads'
```

### Exported torrent metadata

An explicit `--torrent-source` recursively reads `.torrent` files instead of
using `core2.dat`. Tixati's **Share → Save .torrent** action can provide this
metadata. The converter also accepts v1/v2 hybrid torrents.

```bash
python -m tixati_qbittorrent \
  /path/to/tixati /path/to/profile/BT_backup \
  --torrent-source /path/to/exported-torrents \
  --save-path /srv/downloads
```

### Per-torrent manifest

Use a JSON manifest for different save paths, categories, tags, priorities,
trusted piece states, or explicit Unix timestamps:

```json
{
  "defaults": {
    "save_path": "/srv/downloads",
    "category": "migrated",
    "paused": true
  },
  "torrents": [
    {
      "torrent": "exported/example.torrent",
      "save_path": "/srv/downloads/music",
      "tags": ["migrated"],
      "file_priority": [1, 1, 0],
      "piece_state": [1, 0, 1, 1],
      "added_time": 1700000000
    }
  ]
}
```

`piece_state` must contain one `0` or `1` per piece. Omit it unless it came
from a trusted source; omission forces a recheck.

## Library API

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

## Development

```bash
uv lock --check
uv run pytest
```

Maintainer notes and the format-to-code map are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

The project is released under the MIT license.
