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
