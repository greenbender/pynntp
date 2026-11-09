import random
import zlib
from pathlib import Path

import pytest

from nntp.fifo import BytesFifo
from nntp.yenc import YEnc, trailer_crc32


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent.resolve() / "fixtures"


@pytest.fixture
def yenc1_encoded(fixture_dir: Path) -> bytes:
    return (fixture_dir / "yenc1.encoded").read_bytes()


@pytest.fixture
def yenc1_plain(fixture_dir: Path) -> bytes:
    return (fixture_dir / "yenc1.plain").read_bytes()


def test_crc32() -> None:
    assert trailer_crc32(b" crc32=00000000") == 0
    assert trailer_crc32(b" crc32=ffffffff") == 0xFFFFFFFF
    assert trailer_crc32(b" crc32=12345678") == 0x12345678


def test_decode(yenc1_plain: bytes, yenc1_encoded: bytes) -> None:
    fifo = BytesFifo(yenc1_encoded)
    fifo.readline()  # Throw away the first line
    decoder = YEnc()
    plain = b""
    while True:
        line = fifo.readline()
        if line.startswith(b"=yend"):
            break
        plain += decoder.decode(line)
    assert plain == yenc1_plain
    assert decoder.crc32 == 0xDED29F4F


def _reference_decode(buf: bytes) -> bytes:
    """The original byte-at-a-time decoder."""
    data, escape = bytearray(), False
    for b in buf:
        if escape:
            data.append((b - 106) & 0xFF)
            escape = False
        elif b == 0x3D:
            escape = True
        elif b not in {0x0D, 0x0A}:
            data.append((b - 42) & 0xFF)
    return bytes(data)


def test_decode_matches_reference() -> None:
    rng = random.Random(0)
    alphabet = b"=\r\nabc\x00\xff"  # Heavy on escapes and line endings
    for _ in range(500):
        buf = bytes(rng.choice(alphabet) for _ in range(rng.randrange(40)))
        cuts = sorted(rng.sample(range(len(buf) + 1), min(3, len(buf) + 1)))
        decoder = YEnc()
        out = b"".join(
            decoder.decode(buf[a:b]) for a, b in zip([0, *cuts], [*cuts, len(buf)])
        )
        assert out == _reference_decode(buf), buf
        assert decoder.crc32 == zlib.crc32(out)
