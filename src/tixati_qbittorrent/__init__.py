"""Convert Tixati transfer metadata into a qBittorrent profile."""

from .migration import MigrationError, MigrationReport, migrate
from .tixati_core import CoreFormatError, CoreExtraction, CoreTransfer, extract_core_file

__all__ = [
    "CoreExtraction",
    "CoreFormatError",
    "CoreTransfer",
    "MigrationError",
    "MigrationReport",
    "extract_core_file",
    "migrate",
]
