import random
from collections.abc import Iterator
from typing import Any, Callable

import pytest

from nntp.fifo import BytesFifo, TextFifo


def test_readline() -> None:
    fifo = BytesFifo(b"one\r\ntw")
    assert fifo.readline() == b"one\r\n"
    assert fifo.readline() == b""  # Incomplete line stays buffered
    fifo.write(b"o\r\n")
    assert fifo.peekline() == b"two\r\n"
    assert fifo.readline() == b"two\r\n"
    assert len(fifo) == 0


def test_iter() -> None:
    fifo = TextFifo("a\r\nb\r\nc")
    assert list(fifo) == ["a\r\n", "b\r\n"]
    assert fifo.read() == "c"


def test_read_and_peek() -> None:
    fifo = BytesFifo()
    fifo.write(b"abc")
    fifo.write(b"def")
    assert fifo.peek(2) == b"ab"
    assert fifo.read(2) == b"ab"
    assert len(fifo) == 4
    assert fifo.read(10) == b"cdef"  # Over-long read returns the rest
    assert fifo.read() == b""


def test_readuntil() -> None:
    fifo = BytesFifo(b"abcXYdef")
    assert fifo.peekuntil(b"XY") == (True, b"abcXY")
    assert fifo.readuntil(b"XY") == (True, b"abcXY")
    # Token not found: keep len(token) - 1 bytes in case it spans a write.
    assert fifo.readuntil(b"XY") == (False, b"de")
    fifo.write(b"X")
    assert fifo.readuntil(b"XY") == (False, b"f")
    fifo.write(b"Y")
    assert fifo.readuntil(b"XY") == (True, b"XY")


def test_discard_compacts_buffer() -> None:
    fifo = BytesFifo(b"x\r\n" * 100_000)
    assert sum(1 for _ in fifo) == 100_000
    assert len(fifo) == 0


def test_clear() -> None:
    fifo = BytesFifo(b"abc")
    fifo.write(b"def")
    fifo.clear()
    assert fifo.read() == b""


def test_len_counts_unread_writes() -> None:
    fifo = BytesFifo(b"ab")
    fifo.write(b"cde")
    assert len(fifo) == 5
    fifo.readline()
    assert len(fifo) == 5


class Model:
    """Obviously-correct reference: the unread data as one bytes object."""

    def __init__(self) -> None:
        self.data = b""

    def until(self, token: bytes, size: int) -> tuple[bool, int]:
        i = self.data.find(token)
        if i < 0:
            return False, max(len(self.data) - max(len(token) - 1, size), 0)
        return True, i + len(token)


def _to_bytes(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, str):
        return value.encode("latin-1")
    if isinstance(value, tuple):
        return tuple(_to_bytes(v) for v in value)
    return value


class BytesView:
    """Presents a TextFifo through the BytesFifo interface (latin-1 is 1:1)."""

    def __init__(self, fifo: TextFifo) -> None:
        self.fifo = fifo

    def __len__(self) -> int:
        return len(self.fifo)

    def __iter__(self) -> Iterator[bytes]:
        return (line.encode("latin-1") for line in self.fifo)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        method = getattr(self.fifo, name)

        def call(*args: Any) -> Any:  # noqa: ANN401
            args = tuple(
                a.decode("latin-1") if isinstance(a, bytes) else a for a in args
            )
            return _to_bytes(method(*args))

        return call


@pytest.mark.parametrize(
    "make", [BytesFifo, lambda: BytesView(TextFifo())], ids=["bytes", "text"]
)
def test_matches_model(make: Callable[[], Any]) -> None:
    """Random op sequences, crossing the discard threshold, agree with Model."""
    rng = random.Random(0)
    for _ in range(200):
        fifo, model = make(), Model()
        for _ in range(200):
            op = rng.randrange(8)
            n = rng.choice([0, 1, 5, 1000, 70_000])
            if op <= 1:
                data = bytes(rng.choice(b"ab\r\n") for _ in range(rng.randrange(50)))
                data += b"x" * rng.choice([0, 0, 0, 40_000])
                fifo.write(data)
                model.data += data
            elif op == 2:
                expected = model.data[:n] if 0 < n < len(model.data) else model.data
                assert fifo.read(n) == expected
                model.data = model.data[len(expected) :]
            elif op == 3:
                assert fifo.peek(n) == (model.data[:n] if n > 0 else model.data)
            elif op == 4:
                found, end = model.until(b"\r\n", 0)
                assert fifo.peekline() == (model.data[:end] if found else b"")
                assert fifo.readline() == (model.data[:end] if found else b"")
                if found:
                    model.data = model.data[end:]
            elif op == 5:
                found, end = model.until(b"ab", n)
                assert fifo.peekuntil(b"ab", n) == (found, model.data[:end])
                assert fifo.readuntil(b"ab", n) == (found, model.data[:end])
                model.data = model.data[end:]
            elif op == 6:
                assert len(fifo) == len(model.data)
            else:
                lines = model.data.split(b"\r\n")[:-1]
                assert list(fifo) == [line + b"\r\n" for line in lines]
                model.data = model.data[sum(len(x) + 2 for x in lines) :]
        assert fifo.read() == model.data
