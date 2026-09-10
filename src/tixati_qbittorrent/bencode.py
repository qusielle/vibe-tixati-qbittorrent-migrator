"""Small, strict bencode reader/writer used for torrent files and fastresume data.

The implementation deliberately has no third-party dependency.  Torrent metadata
is bencoded, while qBittorrent's ``.fastresume`` files are bencoded dictionaries
written by libtorrent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


BValue: TypeAlias = int | bytes | list["BValue"] | dict[bytes, "BValue"]


class BencodeError(ValueError):
    """Raised when a bencoded value is malformed."""


@dataclass
class _Parser:
    data: bytes
    pos: int = 0

    def parse(self) -> BValue:
        if self.pos >= len(self.data):
            raise BencodeError("unexpected end of input")
        token = self.data[self.pos]
        if token == ord("i"):
            return self._integer()
        if token == ord("l"):
            return self._list()
        if token == ord("d"):
            return self._dictionary()
        if 48 <= token <= 57:
            return self._bytes()
        raise BencodeError(f"invalid token {chr(token)!r} at offset {self.pos}")

    def _integer(self) -> int:
        start = self.pos + 1
        end = self.data.find(b"e", start)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = self.data[start:end]
        if not raw or raw in (b"-", b"-0"):
            raise BencodeError("invalid integer")
        if (raw.startswith(b"0") and len(raw) > 1) or (
            raw.startswith(b"-0")
        ):
            raise BencodeError("non-canonical integer")
        try:
            value = int(raw)
        except ValueError as exc:
            raise BencodeError("invalid integer") from exc
        self.pos = end + 1
        return value

    def _bytes(self) -> bytes:
        colon = self.data.find(b":", self.pos)
        if colon < 0:
            raise BencodeError("missing byte-string separator")
        raw_length = self.data[self.pos:colon]
        if raw_length.startswith(b"0") and len(raw_length) > 1:
            raise BencodeError("non-canonical byte-string length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BencodeError("invalid byte-string length") from exc
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise BencodeError("byte-string extends past end of input")
        self.pos = end
        return self.data[start:end]

    def _list(self) -> list[BValue]:
        self.pos += 1
        result: list[BValue] = []
        while True:
            if self.pos >= len(self.data):
                raise BencodeError("unterminated list")
            if self.data[self.pos] == ord("e"):
                self.pos += 1
                return result
            result.append(self.parse())

    def _dictionary(self) -> dict[bytes, BValue]:
        self.pos += 1
        result: dict[bytes, BValue] = {}
        while True:
            if self.pos >= len(self.data):
                raise BencodeError("unterminated dictionary")
            if self.data[self.pos] == ord("e"):
                self.pos += 1
                return result
            key = self.parse()
            if not isinstance(key, bytes):
                raise BencodeError("dictionary key is not a byte string")
            if key in result:
                raise BencodeError(f"duplicate dictionary key {key!r}")
            result[key] = self.parse()


def decode(data: bytes) -> BValue:
    """Decode exactly one bencoded value."""

    parser = _Parser(data)
    result = parser.parse()
    if parser.pos != len(data):
        raise BencodeError(f"trailing data at offset {parser.pos}")
    return result


def encode(value: BValue) -> bytes:
    """Encode a value using canonical bencode ordering for dictionary keys."""

    if isinstance(value, bool):
        raise TypeError("bool is not a bencode integer")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        parts: list[bytes] = [b"d"]
        for key in sorted(value):
            if not isinstance(key, bytes):
                raise TypeError("bencode dictionary keys must be bytes")
            parts.extend((encode(key), encode(value[key])))
        parts.append(b"e")
        return b"".join(parts)
    raise TypeError(f"unsupported bencode value {type(value).__name__}")

