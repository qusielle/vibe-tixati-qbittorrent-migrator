# Architecture and maintainer notes

This document is a compact handoff guide for contributors who need to extend
the converter or investigate a newer Tixati format.

## Runtime flow

```text
CLI / Python API
        │
        ├─ manifest or exported .torrent files ──> _load_torrent
        │
        └─ core2.dat ──> decrypt_core
                         └─> extract_core
                              └─> CoreTransfer + CoreCategory

all inputs ──> _Torrent ──> _Prepared ──> conflict check ──> atomic writes
                                                               │
                                  profile/BT_backup + profile/categories.json
                                  + optional profile/qBittorrent.conf
```

`migration.py` owns qBittorrent output semantics. `tixati_core.py` owns only
the reverse-engineered Tixati reader. Keeping those layers separate makes it
possible to replace or extend the private-format reader without changing the
resume-file writer.

## `tixati_core.py`

The reader has four deliberate stages:

1. `decrypt_core()` checks the version, applies Tixati's RC4-compatible stream
   transform, and verifies the trailing SHA-1 digest.
2. `_scan_fields()` locates recognized private fields in the decrypted stream.
   It scans rather than recursively decoding every private object because most
   field IDs are undocumented and some values contain nested serialized data.
3. `extract_core()` builds a `_FieldIndex`, then pairs `0x11027` prefixes with
   `0x11003` piece-hash blobs.
   The `pieces<N>:` suffix gives the required blob length, but the final
   association is always accepted only when `SHA1(prefix + pieces + suffix)`
   matches a `0x11009` info hash. It also reads transfer metadata, counters,
   the `0x1103f` Created/Added timestamp, the `0x11040` later activity
   timestamp, and the packed `0x1400a` piece state from the same transfer
   region. Global category maps are scanned separately
   so `d_category` can be paired with its `dlloc` path and the user-category
   registry can be emitted independently of the transfer list.
4. `CoreTransfer.torrent_bytes` wraps the verified raw info dictionary in a
   root torrent dictionary. It embeds `info_bytes` directly rather than using
   `encode(info)`, because bencode key order is part of the info hash.

The recognized field IDs are intentionally centralized in `_scan_fields()`.
When supporting another Tixati version, first add observations and tests for
new IDs there; do not silently reinterpret unknown fields in the migration
layer.

The index is important for large profiles: transfer-region lookups bisect the
positions for one field ID instead of rebuilding and filtering the entire
recognized-field list for every transfer. A 220 MiB real profile dropped from
roughly 94 seconds to roughly 5 seconds in the reference container while
producing the same 3,253 transfers and 10 skipped hash-only records.

`CoreExtraction.skipped_info_hashes` is part of the safety contract. A record
without enough verified metadata must remain visible to the caller rather than
become a guessed torrent.

## `tixati_settings.py`

`settings2.dat` uses the same version, encrypted-body, and SHA-1 envelope as
`core2.dat`, but Tixati uses a separate stream key for it. After decryption,
the body is a counted registry whose entries have typed string or numeric keys
and typed values. `read_settings()` authenticates the complete file before
parsing it into `TixatiSettings`; unknown value types and numeric legacy keys
are retained instead of being assigned an invented meaning.

The settings reader is deliberately independent of qBittorrent output. The
configuration writer performs reviewed mappings for values with compatible
qBittorrent controls, including network encryption/protocol, allocation,
proxy, and interface settings. Values without a safe qBittorrent equivalent
are kept in a migration metadata artifact rather than being mapped to a
different control by guesswork. Explicit CLI arguments override imported
values. Settings import is opt-in through `--write-settings`, so a normal
torrent migration never unexpectedly changes a qBittorrent profile's global
preferences.

## `migration.py`

`_Torrent` is the internal normalized representation. It contains both the
decoded root/info dictionaries needed for validation and the exact raw torrent
bytes that will be written. `_Prepared` adds the generated libtorrent resume
dictionary before any output is touched.

The migration first prepares every entry and computes all destination paths.
Only after checking for conflicts does it call `_atomic_write()`. Each file is
written to a temporary file in the target directory, flushed and fsynced, then
replaced with `os.replace()`.

The generated resume state is intentionally conservative. Tixati's private
piece-completion and path fields are interpreted only when their serialized
shape is unambiguous. The aggregate piece bitmap is expanded into qBittorrent
resume bytes, and a category `dlloc` becomes the default save path. A validated
complete bitmap plus `0x11040` supplies completion/activity dates; unknown
duration or upload-event values stay at zero. The complete user-category map
is rendered to `categories.json`; built-in Tixati status views are excluded.
Explicit CLI paths and path maps still override those values. Transfers remain
paused unless requested otherwise so qBittorrent can safely recheck the data.
The transfer timestamp is passed through `TransferSpec.added_time` and is also
applied to both generated file mtimes.

The public `output_dir` argument is always the qBittorrent profile root. The
writer creates `output_dir/BT_backup` for transfer pairs and, when requested,
`output_dir/qBittorrent.conf` and, when categories exist,
`output_dir/categories.json`; it does not interpret the argument as an
already-created `BT_backup` directory.

## Extending the project safely

- Add format fixtures and focused tests before changing field pairing.
- Preserve raw bencoded `info` bytes whenever a hash is involved.
- Keep the CLI progress callback optional; library calls should remain quiet by
  default.
- Keep new qBittorrent settings opt-in unless their semantics are unambiguous.
- Do not launch Tixati or make network requests as part of conversion.

## Verification

```bash
uv lock --check
uv run pytest
```

The synthetic core fixture in `tests/test_core.py` covers decryption, field
reconstruction, hash-only skipping, raw-info preservation, and direct
migration. Full-profile checks should use a disposable output directory.
