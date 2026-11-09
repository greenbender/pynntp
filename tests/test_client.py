"""Client protocol tests against a scripted fake socket (no server needed)."""

from __future__ import annotations

import gzip
import zlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

import pytest

import nntp
from nntp.headerdict import HeaderDict
from nntp.nntp import NNTPSyncError
from nntp.types import Newsgroup

if TYPE_CHECKING:
    from collections.abc import Iterator

WELCOME = b"200 server ready\r\n"
MODE_READER = b"200 reader mode\r\n"


class FakeSocket:
    """Replays a server byte stream, ``chunk`` bytes per recv, and records sends."""

    last: FakeSocket  # Most recently created, for tests where connect() raises

    def __init__(self, data: bytes, chunk: int) -> None:
        self.data = data
        self.chunk = chunk
        self.sent = b""
        self.sends = 0
        self.closed = False
        FakeSocket.last = self

    def recv(self, size: int) -> bytes:
        out = self.data[: min(size, self.chunk)]
        self.data = self.data[len(out) :]
        return out

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        self.sent += data
        self.sends += 1

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1 if self.closed else 3


@pytest.fixture(params=[1, 7, 4096], ids=lambda c: f"chunk{c}")
def chunk(request: pytest.FixtureRequest) -> int:
    """Run each test with several recv sizes to exercise buffer boundaries."""
    return request.param  # type: ignore[no-any-return]


Connect = Callable[..., "tuple[nntp.NNTPClient, FakeSocket]"]


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch, chunk: int) -> Connect:
    """Return a factory: connect(server_bytes) -> (client, fake_socket).

    Extra positional byte strings are served to later connections, made by
    reconnect().
    """

    def factory(
        server: bytes, *later: bytes, **kwargs: object
    ) -> tuple[nntp.NNTPClient, FakeSocket]:
        socks = [FakeSocket(data, chunk) for data in (server, *later)]
        monkeypatch.setattr(
            "nntp.nntp.socket.create_connection", lambda *_, **__: socks.pop(0)
        )
        sock = socks[0]
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


def dot_stuff(data: bytes) -> bytes:
    """Escape lines starting with "." as an NNTP server does."""
    return b"".join(
        b"." + line if line.startswith(b".") else line
        for line in data.splitlines(keepends=True)
    )


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
    _, sock = connect(
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
    # 0x04 encodes to ".", so each of the first lines starts with a dot.
    plain = (b"\x04" + b"a" * 127) * 4 + bytes(range(256)) * 4
    encoded = yenc_encode(plain)
    assert encoded.startswith(b".")
    size = len(plain)
    server = (
        f"222 1 <a@b>\r\n=ybegin line=128 size={size} name=x\r\n".encode()
        + dot_stuff(encoded)
        + f"=yend size={size} crc32={zlib.crc32(plain):08x}\r\n.\r\n".encode()
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


def _xover_expected() -> list[tuple[int, HeaderDict]]:
    return [
        (1, HeaderDict({"subject": "hello", "from": "me"})),
        (2, HeaderDict({"subject": ".dot", "from": "you"})),
    ]


def _check_xover(result: list[tuple[int, HeaderDict]]) -> None:
    assert result == _xover_expected()  # Names compare case-insensitively


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
        + dot_stuff(yenc_encode(raw))
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


def test_post_bad_header_sends_nothing(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match="Invalid header value"):
        client.post({"Subject": "hi\r\n.\r\nQUIT"}, b"body\n")
    assert sock.sent == b""


def test_post_illegal_chars(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"340 send\r\n" + b"240 ok\r\n")
    with pytest.raises(nntp.NNTPDataError):
        client.post({"Subject": "hi"}, b"good\nba\0d\nlost\n")
    assert b"lost" not in sock.sent


def test_empty_status_line(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"\r\n")
    with pytest.raises(nntp.NNTPProtocolError):
        client.date()


def test_protocol_error_message_is_text(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"bad line\r\n")
    with pytest.raises(nntp.NNTPProtocolError) as info:
        client.date()
    assert str(info.value) == "bad line"


@pytest.mark.parametrize(
    "when",
    [
        datetime(2024, 1, 2, 3, 4, 5),  # noqa: DTZ001 - naive is taken as GMT
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        datetime(2024, 1, 2, 13, 4, 5, tzinfo=timezone(timedelta(hours=10))),
    ],
)
def test_newgroups_and_newnews_send_gmt(connect: Connect, when: datetime) -> None:
    client, sock = connect(
        WELCOME
        + MODE_READER
        + b"231 groups\r\nalt.test 20 10 y\r\n.\r\n"
        + b"230 news\r\n<a@b>\r\n.\r\n"
    )
    sock.sent = b""
    assert list(client.newgroups(when)) == [Newsgroup("alt.test", 10, 20, "y")]
    assert list(client.newnews("alt.*", when)) == ["<a@b>"]
    assert sock.sent == (
        b"NEWGROUPS 20240102 030405 GMT\r\nNEWNEWS alt.* 20240102 030405 GMT\r\n"
    )


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


@pytest.mark.parametrize("verb", ["next", "last"])
def test_next_last(connect: Connect, verb: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"223 7 <a@b> selected\r\n")
    assert getattr(client, verb)() == (7, "<a@b>")
    assert sock.sent.endswith(verb.upper().encode() + b"\r\n")


def test_next_bad_status(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"223 x\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid NEXT status"):
        client.next()


def test_article_bad_status(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"220\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid ARTICLE status"):
        client.article()


@pytest.mark.parametrize("verb", ["hdr", "xhdr"])
def test_hdr(connect: Connect, verb: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"221 ok\r\n1 hello\r\n2\r\n.\r\n")
    assert list(getattr(client, verb)("Subject", (1, 2))) == [(1, "hello"), (2, "")]
    assert sock.sent.endswith(verb.upper().encode() + b" Subject 1-2\r\n")


def test_hdr_bad_line(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"221 ok\r\nx y\r\n.\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid HDR response"):
        list(client.hdr("Subject"))


def test_post_without_message_id(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"340 send\r\n240\r\n")
    assert client.post({"Subject": "hi"}, b"body\n") is True


def test_post_batches_writes(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"340 send\r\n240 ok\r\n")
    sock.sent, sock.sends = b"", 0
    body = b"x" * 98 + b"\n"
    client.post({"Subject": "hi"}, body * 2000)  # About 200 KB
    assert (
        sock.sent
        == b"POST\r\nSubject: hi\r\n\r\n"
        + body.replace(b"\n", b"\r\n") * 2000
        + b".\r\n"
    )
    assert sock.sends < 10  # Not one write per line


def test_bad_welcome_closes_socket(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPTemporaryError):
        connect(b"400 go away\r\n")
    assert FakeSocket.last.closed


def test_mode_reader_failure_closes_socket(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPPermanentError):
        connect(WELCOME + b"502 no reading\r\n")
    assert FakeSocket.last.closed


@pytest.mark.parametrize("method", ["close", "quit"])
def test_exit_after_close_or_quit(connect: Connect, method: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"205 bye\r\n")
    with client:
        getattr(client, method)()
    assert sock.closed


def test_sync_error_is_exported() -> None:
    assert nntp.NNTPSyncError is nntp.nntp.NNTPSyncError


LIST = b"215 list\r\nalt.a 2 1 y\r\nalt.b 2 1 y\r\n.\r\n"
DATE = b"111 20240102030405\r\n"


def test_abandoned_response_breaks_connection(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + LIST + DATE)
    for _group in client.list_active():
        break  # The rest of the response is left unread
    with pytest.raises(NNTPSyncError, match="reconnect"):
        client.date()


def test_unread_response_blocks_commands(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + LIST + DATE)
    client.list("ACTIVE")  # Sent but never iterated: the response is unread
    with pytest.raises(NNTPSyncError):
        client.date()


def test_data_error_part_way_breaks_connection(connect: Connect) -> None:
    server = OVERVIEW_FMT + b"224 ok\r\n1\ta\tb\r\nbad\r\n2\tc\td\r\n.\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPDataError):
        list(client.xover())
    with pytest.raises(NNTPSyncError, match="reconnect"):
        client.date()


def test_connection_lost_breaks_connection(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"111 2024")  # Cut off
    with pytest.raises(nntp.NNTPError, match="Failed to read"):
        client.date()
    with pytest.raises(NNTPSyncError, match="reconnect"):
        client.date()


def test_error_reply_keeps_connection(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"411 no such group\r\n" + DATE)
    with pytest.raises(nntp.NNTPTemporaryError):
        client.group("alt.nope")
    assert client.date() == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_complete_responses_keep_connection(connect: Connect) -> None:
    article = b"220 1 <a@b>\r\nSubject: hi\r\n\r\nbody\r\n.\r\n"
    head = b"221 1 <a@b>\r\nSubject: hi\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + article + head + LIST + DATE)
    assert client.article(1)[2] == b"body\r\n"
    assert client.head(1) == {"subject": "hi"}
    assert len(list(client.list_active())) == 2
    assert client.date().year == 2024


def test_reconnect(connect: Connect) -> None:
    client, first = connect(WELCOME + MODE_READER + LIST, WELCOME + MODE_READER + DATE)
    next(client.list_active())
    with pytest.raises(NNTPSyncError):
        client.date()
    client.reconnect()
    assert first.closed
    assert client.date().year == 2024
    assert FakeSocket.last.sent == b"MODE READER\r\nDATE\r\n"


def test_exit_with_broken_connection_just_closes(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + LIST)
    with client:
        next(client.list_active())
    assert sock.closed
    assert b"QUIT" not in sock.sent


def test_post_send_failure_breaks_connection(connect: Connect) -> None:
    client, _sock = connect(WELCOME + MODE_READER + b"340 send\r\n")

    def body() -> Iterator[bytes]:
        yield b"line\n"
        raise OSError("disk read failed")

    with pytest.raises(OSError, match="disk read failed"):
        client.post({"Subject": "hi"}, body())
    with pytest.raises(NNTPSyncError, match="reconnect"):
        client.date()


def test_commands_after_close(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER)
    client.close()
    with pytest.raises(NNTPSyncError, match="closed"):
        client.date()


def test_reconnect_after_close(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER, WELCOME + MODE_READER + DATE)
    client.close()
    client.reconnect()
    assert client.date().year == 2024


def test_failed_reconnect_stays_broken(connect: Connect) -> None:
    client, _ = connect(
        WELCOME + MODE_READER,
        b"400 try later\r\n",
        WELCOME + MODE_READER + DATE,
    )
    client.close()
    with pytest.raises(nntp.NNTPTemporaryError):
        client.reconnect()
    with pytest.raises(NNTPSyncError):
        client.date()
    client.reconnect()
    assert client.date().year == 2024


def test_decompression_error_part_way_breaks_connection(connect: Connect) -> None:
    server = b"215 list COMPRESS=GZIP\r\n" + b"not gzip data" + b".\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPDataError, match="Decompression failed"):
        list(client.list("ACTIVE"))
    with pytest.raises(NNTPSyncError, match="reconnect"):
        client.date()


def yenc_body(
    plain: bytes,
    *,
    part: int | None = None,
    trailer: str | None = None,
    end: bool = True,
) -> bytes:
    """A BODY response holding plain as yEnc, single or one part of many.

    trailer overrides the =yend keywords (default: correct size and CRC).
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


@pytest.mark.parametrize("part", [None, 2])
def test_yenc_valid(connect: Connect, part: int | None) -> None:
    client, _ = connect(WELCOME + MODE_READER + yenc_body(PLAIN, part=part))
    assert client.body(1) == PLAIN


@pytest.mark.parametrize(
    ("part", "trailer", "match"),
    [
        (None, "size=768 crc32=00000001", "CRC32 is"),
        (None, "size=767 crc32=00000001", "decoded 768 bytes, trailer says 767"),
        (2, "size=768 part=2 pcrc32=00000001", "CRC32 is"),
    ],
)
def test_yenc_mismatch_keeps_connection(
    connect: Connect, part: int | None, trailer: str, match: str
) -> None:
    server = yenc_body(PLAIN, part=part, trailer=trailer) + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPYEncError, match=match) as info:
        client.body(1)
    assert info.value.data == PLAIN  # Kept for e.g. par2 repair
    assert isinstance(info.value, nntp.NNTPDataError)
    assert client.date().year == 2024  # Whole response was read; still in sync


def test_yenc_missing_trailer(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + yenc_body(PLAIN, end=False))
    with pytest.raises(nntp.NNTPDataError, match="missing =yend"):
        client.body(1)


def test_yenc_trailer_without_crc_checks_size_only(connect: Connect) -> None:
    server = yenc_body(PLAIN, trailer="size=768")
    client, _ = connect(WELCOME + MODE_READER + server)
    assert client.body(1) == PLAIN


def test_yenc_article_validated(connect: Connect) -> None:
    body = yenc_body(PLAIN, trailer="size=768 crc32=00000001")
    article = (
        b"220 1 <a@b>\r\nSubject: file yEnc (1/1)\r\n\r\n" + body.split(b"\r\n", 1)[1]
    )
    client, _ = connect(WELCOME + MODE_READER + article)
    with pytest.raises(nntp.NNTPDataError, match="CRC32"):
        client.article(1)
