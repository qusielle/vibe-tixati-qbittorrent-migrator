"""Convert exported Tixati torrent metadata into a qBittorrent profile."""

from .migration import MigrationError, MigrationReport, migrate

__all__ = ["MigrationError", "MigrationReport", "migrate"]

