"""Fake NNTP server socket and response builders for the client tests."""

from __future__ import annotations

import zlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from nntp.headerdict import HeaderDict

if TYPE_CHECKING:
    import nntp

WELCOME = b"200 server ready\r\n"


MODE_READER = b"200 reader mode\r\n"


class FakeSocket:
    """Replays a server byte stream, ``chunk`` bytes per recv, and records sends."""

    last: FakeSocket  # Most recently created, for tests where connect() raises

    def __init__(self, data: bytes, chunk: int) -> None:
        self.data = data
        self.pos = 0  # Bytes of data served so far
        self.chunk = chunk
        self.sent = b""
        self.sends = 0
        self.fail_after: int | None = None  # Raise on sends after this many
        self.closed = False
        FakeSocket.last = self

    def recv(self, size: int) -> bytes:
        out = self.data[self.pos : self.pos + min(size, self.chunk)]
        self.pos += len(out)
        return out

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        if self.fail_after is not None and self.sends >= self.fail_after:
            raise OSError("send failed")
        self.sent += data
        self.sends += 1

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1 if self.closed else 3


Connect = Callable[..., "tuple[nntp.NNTPClient, FakeSocket]"]


def yenc_encode(data: bytes, line_length: int = 128) -> bytes:
    """yEnc-encode data into CRLF-terminated lines (no =ybegin/=yend)."""
    lines, line = [], bytearray()
    for b in data:
        e = (b + 42) & 0xFF
        line += bytes([0x3D, (e + 64) & 0xFF]) if e in b"\0\r\n=" else bytes([e])
        if len(line) >= line_length:
            lines.append(bytes(line) + b"\r\n")
            line = bytearray()
    if line:
        lines.append(bytes(line) + b"\r\n")
    return b"".join(lines)


def dot_stuff(data: bytes) -> bytes:
    """Escape lines starting with "." as an NNTP server does."""
    return b"".join(
        b"." + line if line.startswith(b".") else line
        for line in data.splitlines(keepends=True)
    )


OVERVIEW_FMT = b"215 fmt\r\nSubject:\r\nFrom:\r\n.\r\n"


XOVER_LINES = b"1\thello\tme\r\n2\t.dot\tyou\r\n"


def xover_expected() -> list[tuple[int, HeaderDict]]:
    return [
        (1, HeaderDict({"subject": "hello", "from": "me"})),
        (2, HeaderDict({"subject": ".dot", "from": "you"})),
    ]


def check_xover(result: list[tuple[int, HeaderDict]]) -> None:
    assert result == xover_expected()  # Names compare case-insensitively


def xzver_server(crc: int | None = None) -> bytes:
    deflate = zlib.compressobj(wbits=-15)
    raw = deflate.compress(XOVER_LINES) + deflate.flush()
    crc = zlib.crc32(raw) if crc is None else crc
    return (
        b"224 ok\r\n=ybegin line=128 size=0 name=x\r\n"
        + dot_stuff(yenc_encode(raw))
        + f"=yend size={len(raw)} crc32={crc:08x}\r\n.\r\n".encode()
    )


LIST = b"215 list\r\nalt.a 2 1 y\r\nalt.b 2 1 y\r\n.\r\n"


DATE = b"111 20240102030405\r\n"


def yenc_body(
    plain: bytes,
    *,
    part: int | None = None,
    trailer: str | None = None,
    end: bool = True,
) -> bytes:
    """A BODY response holding plain as yEnc, single or one part of many.

    trailer overrides the =yend keywords (default: correct size and CRC);
    end=False leaves out the =yend line.
    """
    if part is None:
        begin = f"=ybegin line=128 size={len(plain)} name=my file.bin\r\n"
        trailer = trailer or f"size={len(plain)} crc32={zlib.crc32(plain):08x}"
    else:
        begin = f"=ybegin part={part} total=3 line=128 size=99999 name=f\r\n"
        begin += f"=ypart begin=1 end={len(plain)}\r\n"
        trailer = trailer or (
            f"size={len(plain)} part={part} pcrc32={zlib.crc32(plain):08x}"
            " crc32=deadbeef"  # Whole-file CRC: must not be compared to the part
        )
    return (
        b"222 1 <a@b>\r\n"
        + begin.encode()
        + dot_stuff(yenc_encode(plain))
        + (f"=yend {trailer}\r\n".encode() if end else b"")
        + b".\r\n"
    )


PLAIN = bytes(range(256)) * 3


IHAVE_OK = b"335 send it\r\n235 thanks\r\n"


def deflated(*responses: bytes) -> bytes:
    """Server responses as one raw-deflate stream, flushed after each."""
    compressor = zlib.compressobj(wbits=-15)
    return b"".join(
        compressor.compress(r) + compressor.flush(zlib.Z_SYNC_FLUSH) for r in responses
    )


def sent_after_compress(sock: FakeSocket) -> bytes:
    """What the client sent after COMPRESS DEFLATE, decompressed."""
    marker = b"COMPRESS DEFLATE\r\n"
    compressed = sock.sent[sock.sent.index(marker) + len(marker) :]
    return zlib.decompressobj(wbits=-15).decompress(compressed)


COMPRESS_OK = b"206 Compression active\r\n"


ODD = b"299 unexpected\r\n"  # A success code no command expects


def yenczlib(
    code: bytes, payload: bytes, trailer: str | None = None, line_length: int = 128
) -> bytes:
    """An XZVER/XZHDR response: payload deflated, then yEnc-encoded."""
    deflate = zlib.compressobj(wbits=-15)
    raw = deflate.compress(payload) + deflate.flush()
    if trailer is None:
        trailer = f"=yend size={len(raw)} crc32={zlib.crc32(raw):08x}"
    return (
        code
        + b" ok\r\n=ybegin line=128 size=0 name=x\r\n"
        + dot_stuff(yenc_encode(raw, line_length))
        + trailer.encode()
        + b"\r\n.\r\n"
    )
