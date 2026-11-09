"""
Basic yEnc decoder.
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

__all__ = ["YEnc", "trailer_crc32"]


# Some encoders drop leading zeros, so accept 1-8 hex digits.
_CRC32_RE = re.compile(rb"\s+crc(?:32)?=([0-9a-fA-F]{1,8})\b")

# "=" escapes the following byte, whatever it is (including "=", CR, LF).
_ESCAPE_RE = re.compile(b"=(.)", re.DOTALL)
_PLAIN_TABLE = bytes((b - 42) & 0xFF for b in range(256))
_ESCAPED_TABLE = bytes((b - 106) & 0xFF for b in range(256))


def trailer_crc32(trailer: bytes) -> int | None:
    """Extract the CRC32 value from a yEnc trailer."""
    match = _CRC32_RE.search(trailer)
    if not match:
        return None
    return int(match.group(1), 16)


class YEnc:
    """A basic yEnc decoder.

    Keeps track of the CRC32 value as data is decoded.
    """

    def __init__(self) -> None:
        self.crc32 = 0
        self._escape = False

    def decode(self, buf: bytes) -> bytes:
        """Decode a chunk of yEnc data and update crc32.

        CR and LF are removed. An escape split across calls (a chunk ending in
        "=") is completed by the next call.

        Args:
            buf: yEnc encoded data, excluding the =ybegin/=ypart/=yend lines.

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
