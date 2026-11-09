"""COMPRESS DEFLATE (RFC 8054)."""

from __future__ import annotations

import gzip
import zlib
from typing import TYPE_CHECKING

import pytest

import nntp
from tests.helpers import (
    COMPRESS_OK,
    DATE,
    LIST,
    MODE_READER,
    OVERVIEW_FMT,
    WELCOME,
    XOVER_LINES,
    Connect,
    FakeSocket,
    check_xover,
    deflated,
    sent_after_compress,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_compress(connect: Connect) -> None:
    server = COMPRESS_OK + deflated(DATE, LIST, b"340 send\r\n", b"240 <n@x> ok\r\n")
    client, sock = connect(WELCOME + MODE_READER + server)
    client.compress()
    assert client.date().year == 2024
    assert [g.name for g in client.list_active()] == ["alt.a", "alt.b"]
    assert client.post({"Subject": "hi"}, b".body\n") == "<n@x>"
    assert sent_after_compress(sock) == (
        b"DATE\r\nLIST ACTIVE\r\nPOST\r\nSubject: hi\r\n\r\n..body\r\n.\r\n"
    )


def test_compress_refused_stays_plain(connect: Connect) -> None:
    server = b"403 Unable to activate compression\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPTemporaryError):
        client.compress()
    assert client.date().year == 2024  # Plain text, still in sync


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.command("COMPRESS", "DEFLATE"),
        lambda c: c.mode_reader(),
        lambda c: c.command("AUTHINFO USER", "fred"),
        lambda c: c.command("starttls"),
        lambda c: c.login("fred", "pw"),
    ],
    ids=["compress", "mode_reader", "authinfo", "starttls", "login"],
)
def test_forbidden_after_compress(
    connect: Connect, call: Callable[[nntp.NNTPClient], object]
) -> None:
    client, sock = connect(WELCOME + MODE_READER + COMPRESS_OK + deflated(DATE))
    client.compress()
    sent = sock.sent
    with pytest.raises(nntp.NNTPError, match="not allowed after COMPRESS"):
        call(client)
    assert sock.sent == sent  # Nothing was sent
    assert client.date().year == 2024


def test_no_login_after_compress(connect: Connect) -> None:
    server = COMPRESS_OK + deflated(b"480 Authentication required\r\n", DATE)
    client, sock = connect(
        WELCOME + MODE_READER + server, username="fred", password="flintstone"
    )
    client.compress()
    with pytest.raises(nntp.NNTPTemporaryError, match="can't authenticate"):
        client.group("alt.test")
    assert b"AUTHINFO" not in sent_after_compress(sock)
    assert client.date().year == 2024


def test_corrupt_compressed_data_closes(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + COMPRESS_OK + b"\xff\xff\xff\xff")

    def compress_then_date() -> None:
        # compress() raises if the bad bytes arrive with the 206, else date()
        client.compress()
        client.date()

    with pytest.raises(nntp.NNTPDataError, match="Decompression failed"):
        compress_then_date()
    assert sock.closed


def test_reconnect_drops_compression(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + COMPRESS_OK + deflated(DATE),
        WELCOME + MODE_READER + DATE,
    )
    client.compress()
    client.reconnect()
    assert client.date().year == 2024  # Plain text again
    assert FakeSocket.last.sent == b"MODE READER\r\nDATE\r\n"


def test_data_after_end_of_stream_closes(connect: Connect) -> None:
    compressor = zlib.compressobj(wbits=-15)
    ended = compressor.compress(DATE) + compressor.flush()  # Z_FINISH ends it
    server = COMPRESS_OK + ended + deflated(DATE)
    client, sock = connect(WELCOME + MODE_READER + server)

    def compress_then_dates() -> None:
        client.compress()
        client.date()
        client.date()

    with pytest.raises(nntp.NNTPDataError, match="after end of stream"):
        compress_then_dates()
    assert sock.closed


def test_no_compress_over_tls_compression(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.compression = lambda: "zlib"  # type: ignore[attr-defined]
    sent = sock.sent
    with pytest.raises(nntp.NNTPError, match="TLS-level compression"):
        client.compress()
    assert sock.sent == sent


def test_compress_twice(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + COMPRESS_OK)
    client.compress()
    sent = sock.sent
    with pytest.raises(nntp.NNTPError, match="already active"):
        client.compress()
    assert sock.sent == sent


def test_gzip_response_inside_compress(connect: Connect) -> None:
    inner = b"224 ok COMPRESS=GZIP\r\n" + gzip.compress(XOVER_LINES) + b".\r\n"
    server = COMPRESS_OK + deflated(OVERVIEW_FMT, inner)
    client, _ = connect(WELCOME + MODE_READER + server)
    client.compress()
    check_xover(list(client.xover((1, 2))))
