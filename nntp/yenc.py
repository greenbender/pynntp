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

import binascii
import re
import struct
import zlib

__all__ = ["YEnc", "trailer_crc32"]


_crc32_re = re.compile(b"\\s+crc(?:32)?=([0-9a-fA-F]{8})")

# "=" escapes the following byte, whatever it is (including "=", CR, LF).
_escape_re = re.compile(b"=(.)", re.DOTALL)
_plain_table = bytes((b - 42) & 0xFF for b in range(256))
_escaped_table = bytes((b - 106) & 0xFF for b in range(256))


def trailer_crc32(trailer: bytes) -> int | None:
    """Extract the CRC32 value from a yEnc trailer."""
    match = _crc32_re.search(trailer)
    if not match:
        return None
    buf = binascii.unhexlify(match.group(1))
    return struct.unpack(">I", buf)[0]  # type: ignore[no-any-return]


class YEnc:
    """A basic yEnc decoder.

    Keeps track of the CRC32 value as data is decoded.
    """

    def __init__(self) -> None:
        self.crc32 = 0
        self._escape = 0

    def decode(self, buf: bytes) -> bytes:
        prefix = b""
        if self._escape and buf:
            prefix = buf[:1].translate(_escaped_table)
            buf = buf[1:]
            self._escape = 0

        # Alternating [plain, escaped, plain, ..., plain] segments. A plain
        # segment can only contain "=" as its final byte, in the last segment,
        # when the escaped byte is in the next buffer.
        parts = _escape_re.split(buf)
        if parts[-1].endswith(b"="):
            parts[-1] = parts[-1][:-1]
            self._escape = 1
        parts[::2] = [p.translate(_plain_table, b"\r\n") for p in parts[::2]]
        parts[1::2] = [p.translate(_escaped_table) for p in parts[1::2]]

        decoded = prefix + b"".join(parts)
        self.crc32 = zlib.crc32(decoded, self.crc32)
        return decoded
