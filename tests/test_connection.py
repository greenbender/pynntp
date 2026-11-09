"""Connection handling: status lines, login, TLS, sync state and reconnect."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

import nntp
from nntp.nntp import NNTPSyncError
from nntp.types import SSLMode
from tests.helpers import (
    DATE,
    LIST,
    MODE_READER,
    ODD,
    OVERVIEW_FMT,
    WELCOME,
    Connect,
    FakeSocket,
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


def test_command_during_generator(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"215 list\r\nalt.test 2 1 y\r\n.\r\n")
    groups = client.list_active()
    next(groups)
    with pytest.raises(NNTPSyncError):
        client.date()


def test_empty_status_line(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"\r\n")
    with pytest.raises(nntp.NNTPProtocolError):
        client.date()


def test_protocol_error_message_is_text(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"bad line\r\n")
    with pytest.raises(nntp.NNTPProtocolError) as info:
        client.date()
    assert str(info.value) == "bad line"


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


@pytest.mark.parametrize("arg", ["alt.test\r\nQUIT", "alt.test\nQUIT", "alt\0test"])
def test_command_rejects_line_breaks(connect: Connect, arg: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + DATE)
    sock.sent = b""
    with pytest.raises(ValueError, match="CR, LF or NUL"):
        client.group(arg)
    assert sock.sent == b""
    assert client.date().year == 2024  # Nothing was sent; still usable


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


def test_raw_command(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"283 whatever\r\n")
    assert client.command("XSOMETHING", "arg") == (283, "whatever")
    assert sock.sent.endswith(b"XSOMETHING arg\r\n")


@pytest.mark.parametrize(
    ("kwargs", "port"),
    [
        ({}, 119),
        ({"use_ssl": True}, 563),
        ({"use_ssl": True, "ssl_mode": SSLMode.STARTTLS}, 119),
        ({"port": 1119, "use_ssl": True}, 1119),
    ],
)
def test_default_port(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], port: int
) -> None:
    addresses = []

    def refuse(address: tuple[str, int], **_: object) -> FakeSocket:
        addresses.append(address)
        raise ConnectionRefusedError

    monkeypatch.setattr("nntp.nntp.socket.create_connection", refuse)
    with pytest.raises(ConnectionRefusedError):
        nntp.NNTPClient("example.com", **kwargs)  # type: ignore[arg-type]
    assert addresses == [("example.com", port)]
