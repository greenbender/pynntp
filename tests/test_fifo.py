import random

from nntp.fifo import BytesFifo


def test_readline() -> None:
    fifo = BytesFifo(b"one\r\ntw")
    assert fifo.readline() == b"one\r\n"
    assert fifo.readline() == b""  # Incomplete line stays buffered
    fifo.write(b"o\r\n")
    assert fifo.readline() == b"two\r\n"
    assert len(fifo) == 0


def test_iter() -> None:
    fifo = BytesFifo(b"a\r\nb\r\nc")
    assert list(fifo) == [b"a\r\n", b"b\r\n"]
    fifo.write(b"\r\n")
    assert list(fifo) == [b"c\r\n"]  # Iteration resumes after more data


def test_read() -> None:
    fifo = BytesFifo()
    fifo.write(b"abc")
    fifo.write(b"def")
    assert fifo.read(2) == b"ab"
    assert len(fifo) == 4
    assert fifo.read(10) == b"cdef"  # Over-long read returns the rest
    assert fifo.read() == b""


def test_discard_compacts_buffer() -> None:
    fifo = BytesFifo(b"x\r\n" * 100_000)
    assert sum(1 for _ in fifo) == 100_000
    assert len(fifo) == 0


def test_len_counts_unread_writes() -> None:
    fifo = BytesFifo(b"ab")
    fifo.write(b"cde")
    assert len(fifo) == 5
    fifo.readline()
    assert len(fifo) == 5


def test_matches_model() -> None:
    """Random op sequences, crossing the discard threshold, agree with a model.

    The model is the obviously-correct reference: unread data as one bytes.
    """
    rng = random.Random(0)
    for _ in range(200):
        fifo, model = BytesFifo(), b""
        for _ in range(200):
            op = rng.randrange(5)
            n = rng.choice([0, 1, 5, 1000, 70_000])
            if op <= 1:
                data = bytes(rng.choice(b"ab\r\n") for _ in range(rng.randrange(50)))
                data += b"x" * rng.choice([0, 0, 0, 40_000])
                fifo.write(data)
                model += data
            elif op == 2:
                expected = model[:n] if 0 < n < len(model) else model
                assert fifo.read(n) == expected
                model = model[len(expected) :]
            elif op == 3:
                i = model.find(b"\r\n")
                expected = model[: i + 2] if i >= 0 else b""
                assert fifo.readline() == expected
                model = model[len(expected) :]
            else:
                assert len(fifo) == len(model)
                lines = model.split(b"\r\n")[:-1]
                assert list(fifo) == [line + b"\r\n" for line in lines]
                model = model[sum(len(x) + 2 for x in lines) :]
        assert fifo.read() == model
