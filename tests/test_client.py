"""Client protocol tests against a scripted fake socket (no server needed)."""

from __future__ import annotations

import gzip
import zlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import pytest

import nntp
from nntp.nntp import NNTPSyncError

if TYPE_CHECKING:
    from nntp.headerdict import HeaderDict

WELCOME = b"200 server ready\r\n"
MODE_READER = b"200 reader mode\r\n"


class FakeSocket:
    """Replays a server byte stream, ``chunk`` bytes per recv, and records sends."""

    def __init__(self, data: bytes, chunk: int) -> None:
        self.data = data
        self.chunk = chunk
        self.sent = b""

    def recv(self, size: int) -> bytes:
        out = self.data[: min(size, self.chunk)]
        self.data = self.data[len(out) :]
        return out

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        pass


@pytest.fixture(params=[1, 7, 4096], ids=lambda c: f"chunk{c}")
def chunk(request: pytest.FixtureRequest) -> int:
    """Run each test with several recv sizes to exercise buffer boundaries."""
    return request.param  # type: ignore[no-any-return]


Connect = Callable[..., "tuple[nntp.NNTPClient, FakeSocket]"]


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch, chunk: int) -> Connect:
    """Return a factory: connect(server_bytes) -> (client, fake_socket)."""

    def factory(server: bytes, **kwargs: object) -> tuple[nntp.NNTPClient, FakeSocket]:
        sock = FakeSocket(server, chunk)
        monkeypatch.setattr("nntp.nntp.socket.create_connection", lambda *_, **__: sock)
        return nntp.NNTPClient("example.com", **kwargs), sock  # type: ignore[arg-type]

    return factory


def yenc_encode(data: bytes, line_length: int = 128) -> bytes:
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


def test_status_codes(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"111 20240102030405\r\n")
    assert sock.sent == b"MODE READER\r\n"
    assert client.date() == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (b"411 no such group\r\n", nntp.NNTPTemporaryError),
        (b"502 permission denied\r\n", nntp.NNTPPermanentError),
        (b"abc garbage\r\n", nntp.NNTPProtocolError),
        (b"600 out of range\r\n", nntp.NNTPProtocolError),
    ],
)
def test_status_errors(connect: Connect, line: bytes, error: type[Exception]) -> None:
    client, _ = connect(WELCOME + MODE_READER + line)
    with pytest.raises(error):
        client.group("alt.test")


def test_bad_welcome(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPReplyError):
        connect(b"100 not a welcome\r\n")


def test_connection_closed(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPError, match="Failed to read"):
        connect(b"200 truncat")


def test_auth_on_480(connect: Connect) -> None:
    client, sock = connect(
        WELCOME
        + b"480 auth required\r\n"
        + b"381 more auth\r\n"
        + b"281 ok\r\n"
        + b"200 reader mode\r\n",
        username="user",
        password="pass",
    )
    assert sock.sent == (
        b"MODE READER\r\nAUTHINFO USER user\r\nAUTHINFO PASS pass\r\nMODE READER\r\n"
    )


def test_auth_rejected(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPReplyError):
        connect(WELCOME + b"480 auth required\r\n" + b"381 more\r\n" + b"200 nope\r\n")


def test_group(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"211 3 10 12 alt.test\r\n")
    assert client.group("alt.test") == (3, 10, 12, "alt.test")
    assert sock.sent.endswith(b"GROUP alt.test\r\n")


def test_body_dot_unstuffing(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + b"222 1 <a@b>\r\nline\r\n..dotted\r\n.\r\n"
    )
    assert client.body(1) == b"line\r\n.dotted\r\n"


def test_body_yenc_autodetect(connect: Connect) -> None:
    plain = bytes(range(256)) * 4
    server = (
        b"222 1 <a@b>\r\n=ybegin line=128 size=1024 name=x\r\n"
        + yenc_encode(plain)
        + b"=yend size=1024\r\n.\r\n"
    )
    client, _ = connect(WELCOME + MODE_READER + server)
    assert client.body(1) == plain


def test_article(connect: Connect) -> None:
    client, _ = connect(
        WELCOME
        + MODE_READER
        + b"220 5 <a@b>\r\nSubject: hi\r\nFrom: me\r\n\r\nbody\r\n.\r\n"
    )
    articleno, headers, body = client.article(5)
    assert articleno == 5
    assert headers["subject"] == "hi"
    assert body == b"body\r\n"


def test_command_during_generator(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"215 list\r\nalt.test 2 1 y\r\n.\r\n")
    groups = client.list_active()
    next(groups)
    with pytest.raises(NNTPSyncError):
        client.date()


OVERVIEW_FMT = b"215 fmt\r\nSubject:\r\nFrom:\r\n.\r\n"
XOVER_LINES = b"1\thello\tme\r\n2\t.dot\tyou\r\n"


def _xover_expected() -> list[tuple[int, dict[str, str]]]:
    return [
        (1, {"subject": "hello", "from": "me"}),
        (2, {"subject": ".dot", "from": "you"}),
    ]


def _check_xover(result: list[tuple[int, HeaderDict]]) -> None:
    assert [(n, dict(h)) for n, h in result] == _xover_expected()


def test_xover_plain(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + OVERVIEW_FMT + b"224 ok\r\n" + XOVER_LINES + b".\r\n"
    )
    _check_xover(list(client.xover((1, 2))))


@pytest.mark.parametrize("terminator", [False, True])
def test_xover_gzip(connect: Connect, terminator: bool) -> None:
    if terminator:
        payload = gzip.compress(XOVER_LINES + b".\r\n")
    else:
        payload = gzip.compress(XOVER_LINES) + b".\r\n"
    client, _ = connect(
        WELCOME + MODE_READER + OVERVIEW_FMT + b"224 ok COMPRESS=GZIP\r\n" + payload
    )
    _check_xover(list(client.xover((1, 2))))


def _xzver_server(crc: int | None = None) -> bytes:
    deflate = zlib.compressobj(wbits=-15)
    raw = deflate.compress(XOVER_LINES) + deflate.flush()
    crc = zlib.crc32(raw) if crc is None else crc
    return (
        b"224 ok\r\n=ybegin line=128 size=0 name=x\r\n"
        + yenc_encode(raw)
        + f"=yend size={len(raw)} crc32={crc:08x}\r\n.\r\n".encode()
    )


def test_xzver(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + _xzver_server())
    _check_xover(list(client.xzver((1, 2))))


def test_xzver_bad_crc(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + _xzver_server(crc=1))
    with pytest.raises(nntp.NNTPDataError, match="CRC"):
        list(client.xzver((1, 2)))


def test_post(connect: Connect) -> None:
    client, sock = connect(
        WELCOME + MODE_READER + b"340 send\r\n" + b"240 <new@id> posted\r\n"
    )
    sock.sent = b""
    assert client.post({"Subject": "hi"}, b"one\n.two\n") == "<new@id>"
    assert sock.sent == b"POST\r\nSubject: hi\r\n\r\none\r\n..two\r\n.\r\n"


def test_post_illegal_chars(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"340 send\r\n" + b"240 ok\r\n")
    with pytest.raises(nntp.NNTPDataError):
        client.post({"Subject": "hi"}, b"good\nba\0d\nlost\n")
    assert b"lost" not in sock.sent
