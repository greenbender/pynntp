"""
A reasonably efficient FIFO buffer.
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

__all__ = ["BytesFifo"]


_EOL = b"\r\n"

# Consumed data is only dropped once it is both this large and more than half
# the buffer, so the copy cost is amortized O(1) per byte.
_DISCARD_SIZE = 0xFFFF

# A partial line longer than this is moved to the write queue, so it is not
# searched again on every readline() until its CRLF arrives.
_QUEUE_SIZE = 0x1000


class BytesFifo:
    """FIFO byte buffer that reads CRLF-terminated lines.

    Writes are queued and only joined into the buffer once they complete a
    line (or on read()), so many small writes are cheap and a long partial
    line is not copied or searched again on every write. Iterating yields
    complete lines and stops at the first incomplete one; iteration can resume
    after more data is written.
    """

    def __init__(self, data: bytes = b"") -> None:
        self._buf = data
        self._buflist: list[bytes] = []
        self._pending = 0  # Total length of _buflist
        self._eol_queued = False  # _buflist completes a line
        self._pos = 0

    def __len__(self) -> int:
        """Number of unread bytes, including queued writes."""
        return len(self._buf) - self._pos + self._pending

    def __iter__(self) -> BytesFifo:
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def _append(self) -> None:
        if self._buflist:
            self._buf = self._buf[self._pos :] + b"".join(self._buflist)
            self._buflist = []
            self._pending = 0
            self._eol_queued = False
            self._pos = 0

    def _advance(self, newpos: int) -> None:
        if newpos > _DISCARD_SIZE and newpos * 2 > len(self._buf):
            self._buf = self._buf[newpos:]
            self._pos = 0
        else:
            self._pos = newpos

    def write(self, data: bytes) -> None:
        """Append data to the end of the buffer."""
        if not data:
            return
        if not self._eol_queued:
            # The CRLF can also be split between the unread data and this write
            if self._buflist:
                prev = self._buflist[-1]
            else:
                prev = self._buf if self._pos < len(self._buf) else b""
            self._eol_queued = _EOL in data or (
                prev.endswith(b"\r") and data.startswith(b"\n")
            )
        self._buflist.append(data)
        self._pending += len(data)

    def read(self, length: int = 0) -> bytes:
        """Remove and return up to length bytes.

        Args:
            length: Maximum number of bytes to read. A length of 0 (the
                default) reads all buffered data.
        """
        self._append()
        if 0 < length < len(self):
            data = self._buf[self._pos : self._pos + length]
            self._advance(self._pos + length)
            return data
        data = self._buf[self._pos :]
        self._buf = b""
        self._pos = 0
        return data

    def readline(self) -> bytes:
        """Remove and return one line, including its CRLF.

        Returns:
            The line, or b"" if no complete line is buffered yet.
        """
        # Hot path: called once per line of every response.
        buf, pos = self._buf, self._pos
        i = buf.find(_EOL, pos)
        if i < 0:
            if not self._eol_queued:
                if len(buf) - pos > _QUEUE_SIZE:
                    self._buflist.insert(0, buf[pos:])
                    self._pending += len(buf) - pos
                    self._buf, self._pos = b"", 0
                return b""
            self._append()
            buf, pos = self._buf, 0
            i = buf.find(_EOL)
        newpos = i + len(_EOL)
        self._advance(newpos)
        return buf[pos:newpos]
