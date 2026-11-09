"""Client protocol tests against a scripted fake socket (no server needed)."""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

import nntp
from nntp.headerdict import HeaderDict
from nntp.nntp import NNTPSyncError
from nntp.types import Newsgroup, SSLMode

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
        self.fail_after: int | None = None  # Raise on sends after this many
        self.closed = False
        FakeSocket.last = self

    def recv(self, size: int) -> bytes:
        out = self.data[: min(size, self.chunk)]
        self.data = self.data[len(out) :]
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


def test_status_codes(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"111 20240102030405\r\n")
    assert sock.sent == b"MODE READER\r\n"
    assert client.date() == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


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


@pytest.mark.parametrize("body", [b"good\nba\0d\n", b"good\nbare\rcr\n"])
def test_post_illegal_chars_sends_nothing(connect: Connect, body: bytes) -> None:
    client, sock = connect(WELCOME + MODE_READER + DATE)
    sock.sent = b""
    with pytest.raises(nntp.NNTPDataError, match="CR or NUL"):
        client.post({"Subject": "hi"}, body)
    assert sock.sent == b""  # Not even POST: no half-posted article
    assert client.date().year == 2024


def test_post_large_body_spools(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"340 send\r\n240 ok\r\n")
    sock.sent = b""
    body = b"y" * 1000 + b"\n"
    client.post({"Subject": "hi"}, (body for _ in range(3000)))  # ~3 MB, on disk
    assert sock.sent.endswith(body.replace(b"\n", b"\r\n") * 3000 + b".\r\n")


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
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
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


@pytest.mark.parametrize(("verb", "code"), [("hdr", b"225"), ("xhdr", b"221")])
def test_hdr(connect: Connect, verb: str, code: bytes) -> None:
    server = code + b" ok\r\n1 hello\r\n2\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(getattr(client, verb)("Subject", (1, 2))) == [(1, "hello"), (2, "")]
    assert sock.sent.endswith(verb.upper().encode() + b" Subject 1-2\r\n")


@pytest.mark.parametrize(("verb", "code"), [("hdr", b"221"), ("xhdr", b"225")])
def test_hdr_wrong_code(connect: Connect, verb: str, code: bytes) -> None:
    # HDR answers 225 (RFC 3977), XHDR 221 (RFC 2980); the other is an error
    client, _ = connect(WELCOME + MODE_READER + code + b" ok\r\n.\r\n")
    with pytest.raises(nntp.NNTPReplyError):
        list(getattr(client, verb)("Subject"))


def test_hdr_bad_line(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"225 ok\r\nx y\r\n.\r\n")
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
    assert client.date() == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


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


def test_post_body_read_failure_sends_nothing(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + DATE)
    sock.sent = b""

    def body() -> Iterator[bytes]:
        yield b"line\n"
        raise OSError("disk read failed")

    with pytest.raises(OSError, match="disk read failed"):
        client.post({"Subject": "hi"}, body())
    assert sock.sent == b""
    assert client.date().year == 2024


def test_post_send_failure_breaks_connection(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"340 send\r\n")
    sock.fail_after = sock.sends + 1  # POST goes out; the article does not
    with pytest.raises(OSError, match="send failed"):
        client.post({"Subject": "hi"}, b"line\n")
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


@pytest.mark.parametrize(
    ("server", "call"),
    [
        (b"111 not-a-date\r\n", lambda c: c.date()),
        (b"215 list\r\nalt.test x 1 y\r\n.\r\n", lambda c: list(c.list_active())),
        (
            b"231 new\r\nalt.test\r\n.\r\n",
            lambda c: list(c.newgroups(datetime.now(UTC))),
        ),
        (b"221 1 <a@b>\r\nnot a header\r\n.\r\n", lambda c: c.head(1)),
        (b"220 1 <a@b>\r\n continuation\r\n\r\nbody\r\n.\r\n", lambda c: c.article(1)),
    ],
    ids=["date", "list_active", "newgroups", "head", "article"],
)
def test_unparseable_response_raises_data_error(
    connect: Connect, server: bytes, call: Callable[[nntp.NNTPClient], object]
) -> None:
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPDataError, match="Invalid"):
        call(client)


@pytest.mark.parametrize(
    ("arg", "sent"), [((1, 2), b"OVER 1-2"), ("<a@b>", b"OVER <a@b>"), (None, b"OVER")]
)
def test_over(connect: Connect, arg: object, sent: bytes) -> None:
    server = OVERVIEW_FMT + b"224 ok\r\n" + XOVER_LINES + b".\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    _check_xover(list(client.over(arg)))  # type: ignore[arg-type]
    assert sock.sent.endswith(sent + b"\r\n")


@pytest.mark.parametrize(
    ("arg", "sent", "reply"),
    [
        (5, b"STAT 5", b"223 5 <a@b> status"),
        ("<a@b>", b"STAT <a@b>", b"223 0 <a@b> status"),
        (None, b"STAT", b"223 5 <a@b> status"),
    ],
)
def test_stat(connect: Connect, arg: object, sent: bytes, reply: bytes) -> None:
    client, sock = connect(WELCOME + MODE_READER + reply + b"\r\n")
    number, msgid = client.stat(arg)  # type: ignore[arg-type]
    assert (number, msgid) == (int(reply.split()[1]), "<a@b>")
    assert sock.sent.endswith(sent + b"\r\n")


def test_stat_no_article(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"423 no such article\r\n")
    with pytest.raises(nntp.NNTPTemporaryError):
        client.stat(99)


@pytest.mark.parametrize(
    ("name", "numbers", "sent"),
    [
        ("alt.test", None, b"LISTGROUP alt.test"),
        ("alt.test", (2, 3), b"LISTGROUP alt.test 2-3"),
        (None, None, b"LISTGROUP"),
    ],
)
def test_listgroup(
    connect: Connect, name: str | None, numbers: tuple[int, int] | None, sent: bytes
) -> None:
    server = b"211 3 1 3 alt.test list follows\r\n1\r\n2\r\n3\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.listgroup(name, numbers)) == [1, 2, 3]
    assert sock.sent.endswith(sent + b"\r\n")


def test_listgroup_range_needs_name(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match="group name"):
        list(client.listgroup(range=(1, 2)))
    assert sock.sent == b""


def test_listgroup_bad_line(connect: Connect) -> None:
    server = b"211 1 1 1 alt.test\r\nnot-a-number\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPDataError, match="Invalid LISTGROUP line"):
        list(client.listgroup("alt.test"))


IHAVE_OK = b"335 send it\r\n235 thanks\r\n"


def test_ihave(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + IHAVE_OK)
    sock.sent = b""
    client.ihave("<a@b>", {"Subject": "hi"}, b"body\n.dot\n")
    assert sock.sent == (
        b"IHAVE <a@b>\r\nSubject: hi\r\nMessage-ID: <a@b>\r\n\r\nbody\r\n..dot\r\n.\r\n"
    )


def test_ihave_keeps_matching_message_id_header(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + IHAVE_OK)
    sock.sent = b""
    client.ihave("<a@b>", {"message-id": "<a@b>"}, b"x\n")
    assert sock.sent.count(b"<a@b>") == 2  # The command and one header


@pytest.mark.parametrize(
    ("message_id", "headers"),
    [
        ("a@b", {}),
        ("<a b>", {}),
        ("<a@b>\r\nQUIT", {}),
        ("<>", {}),
        ("<a@b>", {"Message-ID": "<other@b>"}),
    ],
)
def test_ihave_invalid_sends_nothing(
    connect: Connect, message_id: str, headers: dict[str, str]
) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match=r"(?i)message-id"):
        client.ihave(message_id, headers, b"x\n")
    assert sock.sent == b""


@pytest.mark.parametrize(
    ("server", "code"),
    [
        (b"435 not wanted\r\n", 435),
        (b"436 try later\r\n", 436),
        (b"335 send it\r\n437 rejected\r\n", 437),
        (b"335 send it\r\n436 failed\r\n", 436),
    ],
)
def test_ihave_refused_keeps_connection(
    connect: Connect, server: bytes, code: int
) -> None:
    client, _ = connect(WELCOME + MODE_READER + server + DATE)
    with pytest.raises(nntp.NNTPTemporaryError) as info:
        client.ihave("<a@b>", {"Subject": "hi"}, b"x\n")
    assert info.value.code == code
    assert client.date().year == 2024


@pytest.mark.parametrize("arg", ["alt.test\r\nQUIT", "alt.test\nQUIT", "alt\0test"])
def test_command_rejects_line_breaks(connect: Connect, arg: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + DATE)
    sock.sent = b""
    with pytest.raises(ValueError, match="CR, LF or NUL"):
        client.group(arg)
    assert sock.sent == b""
    assert client.date().year == 2024  # Nothing was sent; still usable


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
    ],
    ids=["compress", "mode_reader", "authinfo", "starttls"],
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
    _check_xover(list(client.xover((1, 2))))


def test_starttls_rejects_data_after_reply(connect: Connect, chunk: int) -> None:
    # A man-in-the-middle appends a fake reply to the plaintext 382. If it
    # arrives in a later read it goes to the TLS handshake instead, and fails.
    if chunk < 64:
        pytest.skip("The injected reply must arrive in the same read")
    injected = b"200 hi\r\n382 go ahead\r\n211 1 1 1 fake.group\r\n"
    with pytest.raises(nntp.NNTPProtocolError, match="after STARTTLS"):
        connect(injected, use_ssl=True, ssl_mode=SSLMode.STARTTLS)
    assert FakeSocket.last.closed


def test_480_without_username_raises(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"480 auth required\r\n")
    sock.sent = b""
    with pytest.raises(nntp.NNTPTemporaryError) as info:
        client.date()
    assert info.value.code == 480
    assert sock.sent == b"DATE\r\n"  # No bare AUTHINFO USER


def test_command_line_limit(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"211 1 1 1 g\r\n")
    sock.sent = b""
    with pytest.raises(ValueError, match="longer than 512"):
        client.group("g" * 505)  # "GROUP " + 505 + CRLF = 513 octets
    assert sock.sent == b""
    client.group("g" * 504)  # Exactly 512 octets is allowed
    assert len(sock.sent) == 512


def test_overview_fmt_full_is_case_insensitive(connect: Connect) -> None:
    fmt = b"215 fmt\r\nSubject:\r\n:bytes\r\nXref:FULL\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + fmt)
    assert list(client.list_overview_fmt()) == [
        ("Subject", False),
        ("bytes", False),
        ("Xref", True),
    ]


def test_xpat(connect: Connect) -> None:
    server = b"221 matches follow\r\n1 Re: hello\r\n7 hello again\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.xpat("Subject", (1, 9), "*hello*", "*bye*")) == [
        (1, "Re: hello"),
        (7, "hello again"),
    ]
    assert sock.sent.endswith(b"XPAT Subject 1-9 *hello* *bye*\r\n")


def test_xpat_bad_line(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"221 ok\r\nnope\r\n.\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid XPAT response"):
        list(client.xpat("Subject", 1, "*"))


@pytest.mark.parametrize(
    ("terminator", "sent"),
    [(False, b"XFEATURE COMPRESS GZIP"), (True, b"XFEATURE COMPRESS GZIP TERMINATOR")],
)
def test_xfeature_compress_gzip(
    connect: Connect, terminator: bool, sent: bytes
) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"290 feature enabled\r\n")
    client.xfeature_compress_gzip(terminator=terminator)
    assert sock.sent.endswith(sent + b"\r\n")


def test_top_level_exports() -> None:
    assert nntp.SSLMode is SSLMode
    assert nntp.Newsgroup is Newsgroup
    assert nntp.HeaderDict is HeaderDict


def test_body_detection_leading_blank_lines(connect: Connect) -> None:
    yenc = yenc_body(PLAIN).replace(b"222 1 <a@b>\r\n", b"222 1 <a@b>\r\n\r\n\r\n", 1)
    plain = b"222 2 <b@c>\r\n\r\nhello\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + yenc + plain + DATE)
    assert client.body(1) == PLAIN  # Blank lines before yEnc are dropped
    assert client.body(2) == b"\r\nhello\r\n"  # Kept in a plain body
    assert client.date().year == 2024


def test_body_decode_forced(connect: Connect) -> None:
    raw = yenc_encode(b"raw bytes")  # No =ybegin/=yend lines at all
    server = b"222 1 <a@b>\r\n" + raw + b".\r\n" + b"222 2 <b@c>\r\n=ybegin x\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    assert client.body(1, decode=True) == b"raw bytes"
    assert client.body(2, decode=False) == b"=ybegin x\r\n"


def test_body_two_yenc_blocks(connect: Connect) -> None:
    first, second = b"first file", b"second file"
    blocks = b"".join(
        f"=ybegin line=128 size={len(p)} name=f\r\n".encode()
        + yenc_encode(p)
        + f"=yend size={len(p)} crc32={zlib.crc32(p):08x}\r\n".encode()
        for p in (first, second)
    )
    client, _ = connect(WELCOME + MODE_READER + b"222 1 <a@b>\r\n" + blocks + b".\r\n")
    assert client.body(1) == first + second


ODD = b"299 unexpected\r\n"  # A success code no command expects


@pytest.mark.parametrize(
    ("prefix", "call"),
    [
        (b"", lambda c: c.capabilities()),
        (b"", lambda c: c.mode_reader()),
        (b"", lambda c: c.compress()),
        (b"", lambda c: c.quit()),
        (b"", lambda c: c.date()),
        (b"", lambda c: c.help()),
        (b"", lambda c: c.newgroups(datetime.now(UTC))),
        (b"", lambda c: c.newnews("*", datetime.now(UTC))),
        (b"", lambda c: c.list_active()),
        (b"", lambda c: c.group("g")),
        (b"", lambda c: c.listgroup("g")),
        (b"", lambda c: c.next()),
        (b"", lambda c: c.stat(1)),
        (b"", lambda c: c.article(1)),
        (b"", lambda c: c.head(1)),
        (b"", lambda c: c.body(1)),
        (b"", lambda c: c.hdr("Subject")),
        (b"503 no fmt\r\n", lambda c: c.xover()),
        (b"", lambda c: c.xpat("Subject", 1, "*")),
        (b"", lambda c: c.xfeature_compress_gzip()),
        (b"", lambda c: c.post({"Subject": "x"}, b"x\n")),
        (b"", lambda c: c.ihave("<a@b>", {"Subject": "x"}, b"x\n")),
    ],
)
def test_unexpected_reply_raises(
    connect: Connect, prefix: bytes, call: Callable[[nntp.NNTPClient], object]
) -> None:
    client, _ = connect(WELCOME + MODE_READER + prefix + ODD + DATE)

    def run() -> None:
        result = call(client)
        if isinstance(result, Iterator):
            list(result)  # Generators send their command when iterated

    with pytest.raises(nntp.NNTPReplyError) as info:
        run()
    assert info.value.code == 299
    assert client.date().year == 2024  # The status line was read; in sync


def test_help(connect: Connect) -> None:
    server = b"100 help follows\r\nARTICLE\r\nBODY\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    assert client.help() == "ARTICLE\r\nBODY\r\n"


def test_list_active_times(connect: Connect) -> None:
    server = b"215 times\r\nalt.test 1641048001 news@example.com\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.list_active_times()) == [
        ("alt.test", datetime(2022, 1, 1, 14, 40, 1, tzinfo=UTC), "news@example.com")
    ]
    assert sock.sent.endswith(b"LIST ACTIVE.TIMES\r\n")


@pytest.mark.parametrize("line", [b"alt.test", b"alt.test notatime who"])
def test_list_active_times_bad_line(connect: Connect, line: bytes) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"215 times\r\n" + line + b"\r\n.\r\n")
    with pytest.raises(nntp.NNTPDataError, match=r"ACTIVE\.TIMES"):
        list(client.list_active_times())


@pytest.mark.parametrize(
    ("variant", "sent"), [(None, b"LIST HEADERS"), ("MSGID", b"LIST HEADERS MSGID")]
)
def test_list_headers(connect: Connect, variant: str | None, sent: bytes) -> None:
    server = b"215 headers\r\nSubject\r\n:bytes\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.list_headers(variant)) == ["Subject", ":bytes"]  # type: ignore[arg-type]
    assert sock.sent.endswith(sent + b"\r\n")


def test_list_extensions(connect: Connect) -> None:
    server = b"202 extensions\r\nOVER\r\nHDR\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + server)
    assert list(client.list_extensions()) == ["OVER", "HDR"]
    assert sock.sent.endswith(b"LIST EXTENSIONS\r\n")


@pytest.mark.parametrize("line", [b"Subject:extra:", b"Subject:bogus"])
def test_overview_fmt_bad_line(connect: Connect, line: bytes) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"215 fmt\r\n" + line + b"\r\n.\r\n")
    with pytest.raises(nntp.NNTPDataError, match=r"OVERVIEW\.FMT"):
        list(client.list_overview_fmt())


def test_group_bad_reply(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"211 x y z alt.test\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid GROUP"):
        client.group("alt.test")


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
        (yenczlib(b"224", XOVER_LINES, trailer="=yend size=1"), "Bad yEnc trailer"),
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


def test_exit_when_quit_fails_closes_and_raises(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"400 cannot quit\r\n")
    with pytest.raises(nntp.NNTPTemporaryError), client:
        pass
    assert sock.closed


def test_auth_user_alone_accepted(connect: Connect) -> None:
    server = WELCOME + b"480 auth\r\n" + b"281 ok\r\n" + MODE_READER
    _, sock = connect(server, username="fred")
    assert sock.sent == b"MODE READER\r\nAUTHINFO USER fred\r\nMODE READER\r\n"


def test_auth_unexpected_reply(connect: Connect) -> None:
    server = WELCOME + b"480 auth\r\n" + b"381 pass\r\n" + b"250 odd\r\n"
    with pytest.raises(nntp.NNTPReplyError) as info:
        connect(server, username="fred", password="pw")
    assert info.value.code == 250


def test_no_reader_mode(connect: Connect) -> None:
    client, sock = connect(WELCOME + DATE, reader=False)
    assert client.date().year == 2024
    assert sock.sent == b"DATE\r\n"


def test_list_without_keyword(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + LIST)
    assert len(list(client.list())) == 2
    assert sock.sent.endswith(b"MODE READER\r\nLIST\r\n")


def test_list_newsgroups_without_description(connect: Connect) -> None:
    server = b"215 groups\r\nalt.test A test group\r\nalt.bare\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    assert list(client.list_newsgroups()) == [
        ("alt.test", "A test group"),
        ("alt.bare", ""),
    ]


def test_body_only_blank_lines(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"222 1 <a@b>\r\n\r\n\r\n.\r\n")
    assert client.body(1) == b"\r\n\r\n"


def test_xover_short_line_missing_full_field(connect: Connect) -> None:
    fmt = b"215 fmt\r\nSubject:\r\nXref:full\r\n.\r\n"
    server = fmt + b"224 ok\r\n1\ts1\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    assert list(client.xover((1,))) == [(1, HeaderDict({"subject": "s1"}))]


def test_post_unexpected_final_reply(connect: Connect) -> None:
    server = b"340 send\r\n" + ODD + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPReplyError) as info:
        client.post({"Subject": "x"}, b"x\n")
    assert info.value.code == 299
    assert client.date().year == 2024


def test_xzver_tiny_lines(connect: Connect) -> None:
    # One byte per yEnc line: many lines inflate to nothing on their own
    server = yenczlib(b"224", XOVER_LINES, line_length=1)
    client, _ = connect(WELCOME + MODE_READER + OVERVIEW_FMT + server)
    _check_xover(list(client.xzver((1, 2))))


def test_raw_command(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"283 whatever\r\n")
    assert client.command("XSOMETHING", "arg") == (283, "whatever")
    assert sock.sent.endswith(b"XSOMETHING arg\r\n")
