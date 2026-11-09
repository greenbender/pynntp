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

from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = ["BytesFifo", "Fifo", "TextFifo"]


# Consumed data is only dropped once it is both this large and more than half
# the buffer, so the copy cost is amortized O(1) per byte.
_DISCARD_SIZE = 0xFFFF


T = TypeVar("T", str, bytes)


class Fifo(Generic[T]):
    empty: T
    eol: T

    def __init__(self, data: T | None = None) -> None:
        self.buf: T = data or self.empty
        self.buflist: list[T] = []
        self.pending = 0  # Total length of buflist
        self.pos = 0

    def __len__(self) -> int:
        return len(self.buf) - self.pos + self.pending

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> T:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def _append(self) -> None:
        if self.buflist:
            self.buf = self.buf[self.pos :] + self.empty.join(self.buflist)
            self.buflist = []
            self.pending = 0
            self.pos = 0

    def _advance(self, newpos: int) -> None:
        if newpos > _DISCARD_SIZE and newpos * 2 > len(self.buf):
            self.buf = self.buf[newpos:]
            self.pos = 0
        else:
            self.pos = newpos

    def _find(self, token: T, size: int) -> tuple[bool, int]:
        """Return (found, end position) of the data up to and including token.

        If token is not found the end position holds back enough data that a
        token split across writes can still be found later.
        """
        self._append()
        i = self.buf.find(token, self.pos)
        if i < 0:
            index = max(len(token) - 1, size)
            return False, max(len(self.buf) - index, self.pos)
        return True, i + len(token)

    def clear(self) -> None:
        self.buf = self.empty
        self.buflist = []
        self.pending = 0
        self.pos = 0

    def write(self, data: T) -> None:
        self.buflist.append(data)
        self.pending += len(data)

    def read(self, length: int = 0) -> T:
        self._append()
        if 0 < length < len(self):
            data = self.buf[self.pos : self.pos + length]
            self._advance(self.pos + length)
            return data
        data = self.buf[self.pos :]
        self.clear()
        return data

    def readline(self) -> T:
        # Hot path: called once per line of every response.
        if self.buflist:
            self._append()
        buf, pos = self.buf, self.pos
        i = buf.find(self.eol, pos)
        if i < 0:
            return self.empty
        newpos = i + len(self.eol)
        self._advance(newpos)
        return buf[pos:newpos]

    def readuntil(self, token: T, size: int = 0) -> tuple[bool, T]:
        found, newpos = self._find(token, size)
        data = self.buf[self.pos : newpos]
        self._advance(newpos)
        return found, data

    def peek(self, length: int = 0) -> T:
        self._append()
        if length > 0:
            return self.buf[self.pos : self.pos + length]
        return self.buf[self.pos :]

    def peekline(self) -> T:
        found, newpos = self._find(self.eol, 0)
        return self.buf[self.pos : newpos] if found else self.empty

    def peekuntil(self, token: T, size: int = 0) -> tuple[bool, T]:
        found, newpos = self._find(token, size)
        return found, self.buf[self.pos : newpos]


class TextFifo(Fifo[str]):
    empty = ""
    eol = "\r\n"


class BytesFifo(Fifo[bytes]):
    empty = b""
    eol = b"\r\n"
