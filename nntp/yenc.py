"""
yEnc decoding and trailer checks.
Copyright (C) 2013-2020  Byron Platt

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import re
import zlib

__all__ = ["YEnc", "check_trailer", "parse_keywords", "trailer_crc32"]


_KEYWORD_RE = re.compile(rb"(\w+)=(\S*)")
# Some encoders drop leading zeros, so accept 1-8 hex digits.
_CRC32_RE = re.compile(r"[0-9a-fA-F]{1,8}")
_DIGITS_RE = re.compile(r"[0-9]+")

# "=" escapes the following byte, whatever it is (including "=", CR, LF).
_ESCAPE_RE = re.compile(b"=(.)", re.DOTALL)
_PLAIN_TABLE = bytes((b - 42) & 0xFF for b in range(256))
_ESCAPED_TABLE = bytes((b - 106) & 0xFF for b in range(256))


def parse_keywords(line: bytes) -> dict[str, str]:
    """Parse the keyword=value pairs of a =ybegin, =ypart or =yend line.

    The name keyword (on =ybegin) runs to the end of the line and may contain
    spaces and "=", so it and anything after it is left out.
    """
    line = line.split(b" name=", 1)[0]
    return {
        key.decode("ascii"): value.decode("ascii", "replace")
        for key, value in _KEYWORD_RE.findall(line)
    }


def _crc_key(keywords: dict[str, str], *, part: bool) -> str:
    """The trailer keyword holding the CRC: pcrc32 for a part, else crc32 or crc."""
    if part:
        return "pcrc32"
    return "crc32" if "crc32" in keywords else "crc"


def trailer_crc32(trailer: bytes, *, part: bool = False) -> int | None:
    """Extract the CRC32 value from a yEnc trailer.

    Args:
        trailer: The =yend line.
        part: Return the CRC of this part (pcrc32) rather than of the whole
            file (crc32), for multipart posts.
    """
    keywords = parse_keywords(trailer)
    value = keywords.get(_crc_key(keywords, part=part))
    if value is None or not _CRC32_RE.fullmatch(value):
        return None
    return int(value, 16)


def check_trailer(trailer: bytes, size: int, crc32: int, *, part: bool) -> str | None:
    """Check decoded data against a =yend trailer.

    Args:
        trailer: The =yend line.
        size: Length of the decoded data.
        crc32: CRC32 of the decoded data.
        part: The data is one part of a multipart post, so it is checked
            against pcrc32 rather than the whole-file crc32.

    Returns:
        A description of the first problem, or None if the data matches. The
        size is required; a CRC is optional but must be valid if present.
    """
    keywords = parse_keywords(trailer)
    expected_size = keywords.get("size", "")
    if not _DIGITS_RE.fullmatch(expected_size):
        return "trailer has no valid size"
    if int(expected_size) != size:
        return f"decoded {size} bytes, trailer says {expected_size}"
    if "part" in keywords and not _DIGITS_RE.fullmatch(keywords["part"]):
        return "trailer has an invalid part"
    key = _crc_key(keywords, part=part or "part" in keywords)
    value = keywords.get(key)
    if value is not None and not _CRC32_RE.fullmatch(value):
        return f"trailer has an invalid {key}"
    if value is not None and int(value, 16) != crc32:
        return f"CRC32 is {crc32:08x}, trailer says {int(value, 16):08x}"
    return None


class YEnc:
    """A yEnc decoder that keeps a running CRC32 of the decoded data."""

    def __init__(self) -> None:
        self.crc32 = 0
        self._escape = False

    def decode(self, buf: bytes) -> bytes:
        """Decode a chunk of yEnc data and update crc32.

        CR and LF are removed. An escape split across calls (a chunk ending in
        "=") is completed by the next call.

        Args:
            buf: yEnc data, without the =ybegin, =ypart and =yend lines.

        Returns:
            The decoded bytes.
        """
        if self._escape:
            buf = b"=" + buf
            self._escape = False

        # Alternating [plain, escaped, plain, ..., plain] segments. A plain
        # segment can only contain "=" as its final byte, in the last segment,
        # when the escaped byte is in the next buffer.
        parts = _ESCAPE_RE.split(buf)
        if parts[-1].endswith(b"="):
            parts[-1] = parts[-1][:-1]
            self._escape = True
        parts[::2] = [p.translate(_PLAIN_TABLE, b"\r\n") for p in parts[::2]]
        parts[1::2] = [p.translate(_ESCAPED_TABLE) for p in parts[1::2]]

        decoded = b"".join(parts)
        self.crc32 = zlib.crc32(decoded, self.crc32)
        return decoded
