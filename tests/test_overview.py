"""XOVER, OVER and the compressed XZVER/XZHDR and gzip responses."""

from __future__ import annotations

import gzip

import pytest

import nntp
from nntp.headerdict import HeaderDict
from tests.helpers import (
    DATE,
    MODE_READER,
    OVERVIEW_FMT,
    WELCOME,
    XOVER_LINES,
    Connect,
    check_xover,
    xzver_server,
    yenc_encode,
    yenczlib,
)


def test_xover_plain(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + OVERVIEW_FMT + b"224 ok\r\n" + XOVER_LINES + b".\r\n"
    )
    check_xover(list(client.xover((1, 2))))


@pytest.mark.parametrize("terminator", [False, True])
def test_xover_gzip(connect: Connect, terminator: bool) -> None:
    if terminator:
        payload = gzip.compress(XOVER_LINES + b".\r\n")
    else:
        payload = gzip.compress(XOVER_LINES) + b".\r\n"
    client, _ = connect(
        WELCOME + MODE_READER + OVERVIEW_FMT + b"224 ok COMPRESS=GZIP\r\n" + payload
    )
    check_xover(list(client.xover((1, 2))))


def test_xzver(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + xzver_server())
    check_xover(list(client.xzver((1, 2))))


def test_xzver_bad_crc(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + xzver_server(crc=1))
    with pytest.raises(nntp.NNTPDataError, match="CRC"):
        list(client.xzver((1, 2)))


def test_xover_full_field_and_empty_trailing(connect: Connect) -> None:
    fmt = b"215 fmt\r\nSubject:\r\nXref:full\r\nLines:\r\n.\r\n"
    lines = b"1\ts1\tXref: host alt.test:1\t5\r\n2\ts2\t\t\r\n"
    client, _ = connect(WELCOME + MODE_READER + fmt + b"224 ok\r\n" + lines + b".\r\n")
    assert list(client.xover((1, 2))) == [
        (1, HeaderDict({"subject": "s1", "xref": "host alt.test:1", "lines": "5"})),
        (2, HeaderDict({"subject": "s2", "xref": "", "lines": ""})),
    ]


def test_xover_default_fmt(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + b"503 no fmt\r\n" + b"224 ok\r\n1\ts\tf\r\n.\r\n"
    )
    assert list(client.xover()) == [(1, HeaderDict({"Subject": "s", "From": "f"}))]
    assert client.overview_fmt[:2] == ("Subject", "From")


def test_xover_bad_line(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER + OVERVIEW_FMT + b"224 ok\r\nx\ty\r\n.\r\n"
    )
    with pytest.raises(nntp.NNTPDataError, match="Invalid XOVER response"):
        list(client.xover())


@pytest.mark.parametrize(
    ("arg", "sent"), [((1, 2), b"OVER 1-2"), ("<a@b>", b"OVER <a@b>"), (None, b"OVER")]
)
def test_over(connect: Connect, arg: object, sent: bytes) -> None:
    server = OVERVIEW_FMT + b"224 ok\r\n" + XOVER_LINES + b".\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    check_xover(list(client.over(arg)))  # type: ignore[arg-type]
    assert sock.sent.endswith(sent + b"\r\n")


def test_xzhdr(connect: Connect) -> None:
    server = yenczlib(b"221", b"1 hello\r\n2 world\r\n")
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.xzhdr("Subject", (1, 2))) == [(1, "hello"), (2, "world")]
    assert sock.sent.endswith(b"XZHDR Subject 1-2\r\n")


@pytest.mark.parametrize(
    ("server", "match"),
    [
        (b"224 ok\r\nnot a ybegin line\r\n.\r\n", "Bad yEnc header"),
        (b"224 ok\r\n=ybegin line=128 size=0 name=x\r\n.\r\n", "Missing yEnc trailer"),
        (yenczlib(b"224", XOVER_LINES, trailer="=yend size=1"), "trailer says 1"),
        (
            b"224 ok\r\n=ybegin line=128 size=0 name=x\r\n"
            + yenc_encode(b"\xff\xff not deflate")
            + b"=yend size=1 crc32=00000000\r\n.\r\n",
            "Decompression failed",
        ),
    ],
)
def test_xzver_errors(connect: Connect, server: bytes, match: str) -> None:
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + server)
    with pytest.raises(nntp.NNTPDataError, match=match):
        list(client.xzver((1, 2)))


def test_xover_short_line_missing_full_field(connect: Connect) -> None:
    fmt = b"215 fmt\r\nSubject:\r\nXref:full\r\n.\r\n"
    server = fmt + b"224 ok\r\n1\ts1\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    assert list(client.xover((1,))) == [(1, HeaderDict({"subject": "s1"}))]


def test_xzver_tiny_lines(connect: Connect) -> None:
    # One byte per yEnc line: many lines inflate to nothing on their own
    server = yenczlib(b"224", XOVER_LINES, line_length=1)
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + server)
    check_xover(list(client.xzver((1, 2))))


def test_xzver_empty_response(connect: Connect) -> None:
    server = OVERVIEW_FMT + b"224 none\r\n.\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    assert list(client.xzver((1, 2))) == []
    assert client.date().year == 2024  # Read to the end; still in sync


def test_xzhdr_empty_response(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"221 none\r\n.\r\n" + DATE)
    assert list(client.xzhdr("Subject", (1, 2))) == []
    assert client.date().year == 2024


def test_overview_fmt_temporary_error_not_cached(connect: Connect) -> None:
    server = b"400 busy\r\n" + OVERVIEW_FMT + b"224 ok\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPTemporaryError):
        list(client.xover())
    assert list(client.xover()) == []  # Retried, not cached as the default
