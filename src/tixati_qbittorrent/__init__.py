"""Convert Tixati transfer metadata into a qBittorrent profile."""

from .migration import MigrationError, MigrationReport, migrate
from .model import CategorySpec
from .tixati_core import (
    CoreCategory,
    CoreFormatError,
    CoreExtraction,
    CoreTransfer,
    extract_core_file,
)

__all__ = [
    "CoreExtraction",
    "CoreCategory",
    "CoreFormatError",
    "CoreTransfer",
    "CategorySpec",
    "MigrationError",
    "MigrationReport",
    "extract_core_file",
    "migrate",
]
