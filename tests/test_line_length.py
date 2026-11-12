"""The maximum line length, checked on the received and on the decompressed data."""

from __future__ import annotations

import gzip

import pytest

import nntp
from tests.helpers import (
    COMPRESS_OK,
    DATE,
    MODE_READER,
    WELCOME,
    Connect,
    FakeSocket,
    deflated,
    yenczlib,
)

DEFAULT = 0x10000  # The default max_line_length

# How a multi-line response reaches the client: as is, gzip-compressed (with
# the terminator after or inside the gzip data), yEnc-encoded zlib (XZVER), or
# on a COMPRESS DEFLATE connection.
KINDS = ["plain", "gzip", "gzip-terminator", "xz", "deflate"]


def line(length: int, prefix: bytes = b"") -> bytes:
    """A line of exactly length octets, including its CRLF."""
    return prefix + b"x" * (length - len(prefix) - 2) + b"\r\n"


def respond(kind: str, payload: bytes) -> bytes:
    """What the server sends after MODE READER: a 224 response, then DATE."""
    if kind == "gzip":
        response = b"224 ok COMPRESS=GZIP\r\n" + gzip.compress(payload) + b".\r\n"
    elif kind == "gzip-terminator":
        response = b"224 ok COMPRESS=GZIP\r\n" + gzip.compress(payload + b".\r\n")
    elif kind == "xz":
        response = yenczlib(b"224", payload)
    else:
        response = b"224 ok\r\n" + payload + b".\r\n"
    if kind == "deflate":
        return COMPRESS_OK + deflated(response, DATE)
    return response + DATE


def read_response(client: nntp.NNTPClient, kind: str) -> str:
    if kind == "deflate":
        client.compress()
    code, message = client.command("XOVER")
    return "".join(client.info(code, message, yz=kind == "xz"))


def assert_broken(client: nntp.NNTPClient) -> None:
    with pytest.raises(nntp.NNTPSyncError):
        client.date()


@pytest.mark.parametrize("kind", KINDS)
def test_lines_at_limit(connect: Connect, kind: str) -> None:
    # Over 64 KiB at a time from the decompressors, so output is also pending
    payload = b"short\r\n" + line(DEFAULT) * 3 + b"short\r\n"
    client, _ = connect(WELCOME + MODE_READER + respond(kind, payload))
    assert read_response(client, kind) == payload.decode()
    assert client.date().year == 2024


@pytest.mark.parametrize("kind", KINDS)
def test_many_short_lines(connect: Connect, kind: str) -> None:
    payload = b"".join(b"%d\tsubject %d\r\n" % (i, i) for i in range(5_000))
    client, _ = connect(WELCOME + MODE_READER + respond(kind, payload))
    assert read_response(client, kind) == payload.decode()
    assert client.date().year == 2024


@pytest.mark.parametrize("kind", KINDS)
def test_line_too_long(connect: Connect, kind: str) -> None:
    payload = b"short\r\n" + line(DEFAULT + 1) + b"short\r\n"
    client, _ = connect(WELCOME + MODE_READER + respond(kind, payload))
    with pytest.raises(nntp.NNTPDataError, match=f"Line longer than {DEFAULT} octets"):
        read_response(client, kind)
    assert_broken(client)


@pytest.mark.parametrize("kind", KINDS)
def test_long_line_is_not_read(connect: Connect, kind: str) -> None:
    # 16 MiB without CRLF: about 16 KB on the wire when compressed
    server = WELCOME + MODE_READER + respond(kind, b"x" * (16 << 20))
    client, sock = connect(server)
    with pytest.raises(nntp.NNTPDataError, match="Line longer than"):
        read_response(client, kind)
    assert sock.pos < len(server) // 2  # It stopped reading
    assert_broken(client)


@pytest.mark.parametrize(("length", "ok"), [(DEFAULT, True), (DEFAULT + 1, False)])
def test_status_line(connect: Connect, length: int, ok: bool) -> None:
    reply = line(length, b"299 ")
    client, _ = connect(WELCOME + MODE_READER + reply + DATE)
    if ok:
        assert client.command("X") == (299, reply[4:-2].decode())
        assert client.date().year == 2024
    else:
        with pytest.raises(nntp.NNTPDataError, match="Line longer than"):
            client.command("X")
        assert_broken(client)


def test_status_line_is_not_read(connect: Connect) -> None:
    server = WELCOME + MODE_READER + b"299 " + b"x" * (1 << 20)
    client, sock = connect(server)
    with pytest.raises(nntp.NNTPDataError, match="Line longer than"):
        client.command("X")
    assert sock.pos <= len(WELCOME + MODE_READER) + DEFAULT + 4096
    assert_broken(client)


@pytest.mark.parametrize(("length", "ok"), [(100, True), (101, False)])
def test_limit_set_on_class(
    connect: Connect, monkeypatch: pytest.MonkeyPatch, length: int, ok: bool
) -> None:
    monkeypatch.setattr(nntp.NNTPClient, "max_line_length", 100)
    welcome = line(length, b"200 ")
    if ok:
        client, _ = connect(welcome + MODE_READER)
        assert client.max_line_length == 100
    else:
        with pytest.raises(nntp.NNTPDataError, match="Line longer than 100 octets"):
            connect(welcome + MODE_READER)
        assert FakeSocket.last.closed


@pytest.mark.parametrize(("length", "ok"), [(100, True), (101, False)])
def test_limit_set_on_instance(connect: Connect, length: int, ok: bool) -> None:
    body = b"short\r\n" + line(length) + b"short\r\n"
    client, _ = connect(WELCOME + MODE_READER + b"222 1 <a@b>\r\n" + body + b".\r\n")
    client.max_line_length = 100
    if ok:
        assert client.body(1, decode=False) == body
    else:
        with pytest.raises(nntp.NNTPDataError, match="Line longer than 100 octets"):
            client.body(1, decode=False)
        assert_broken(client)
