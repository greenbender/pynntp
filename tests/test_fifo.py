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
