"""Connection handling: status lines, login, TLS, sync state and reconnect."""

from __future__ import annotations

import gc
import ssl
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from collections.abc import Callable


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
        (b"\r\n", nntp.NNTPProtocolError),
        (b"0411 leading zero\r\n", nntp.NNTPProtocolError),
        (b"+411 sign\r\n", nntp.NNTPProtocolError),
        (b"4111 four digits\r\n", nntp.NNTPProtocolError),
        (b"099 too low\r\n", nntp.NNTPProtocolError),
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
        WELCOME + b"480 auth required\r\n" + b"381 more auth\r\n" + b"281 ok\r\n",
        username="user",
        password="pass",
    )
    # RFC 4643 2.2: no MODE READER after logging in
    assert sock.sent == b"MODE READER\r\nAUTHINFO USER user\r\nAUTHINFO PASS pass\r\n"


def test_command_during_generator(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"215 list\r\nalt.test 2 1 y\r\n.\r\n")
    client.list_active()  # Sent now; its response is still unread
    with pytest.raises(NNTPSyncError, match="reconnect"):
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


@pytest.mark.parametrize("reply", [b"502 no reading\r\n", b"480 auth\r\n"])
def test_mode_reader_failure_closes_socket(connect: Connect, reply: bytes) -> None:
    with pytest.raises(nntp.NNTPReplyError):
        connect(WELCOME + reply)  # 480 without a username: no login
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


@pytest.mark.parametrize("chunk", [4096])  # The injected reply must arrive in one read
def test_starttls_rejects_data_after_reply(connect: Connect) -> None:
    # A man-in-the-middle appends a fake reply to the plaintext 382. If it
    # arrives in a later read it goes to the TLS handshake instead, and fails.
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
        (b"", lambda c: c.login("fred")),
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

    with pytest.raises(nntp.NNTPReplyError) as info:
        call(client)  # Iterator methods send their command when called
    assert info.value.code == 299
    assert client.date().year == 2024  # The status line was read; in sync


def test_exit_when_quit_fails_closes_and_raises(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"400 cannot quit\r\n")
    with pytest.raises(nntp.NNTPTemporaryError), client:
        pass
    assert sock.closed


def test_auth_user_alone_accepted(connect: Connect) -> None:
    server = WELCOME + b"480 auth\r\n" + b"281 ok\r\n"
    _, sock = connect(server, username="fred")
    assert sock.sent == b"MODE READER\r\nAUTHINFO USER fred\r\n"


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


def test_starttls_480_never_logs_in(connect: Connect) -> None:
    with pytest.raises(nntp.NNTPTemporaryError, match="before STARTTLS"):
        connect(
            WELCOME + b"480 auth first\r\n",
            use_ssl=True,
            ssl_mode=SSLMode.STARTTLS,
            username="user",
            password="pass",
        )
    assert FakeSocket.last.sent == b"STARTTLS\r\n"  # No password in clear text


def test_login(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"381 more\r\n" + b"281 ok\r\n")
    sock.sent = b""
    client.login("user", "pass")
    assert sock.sent == b"AUTHINFO USER user\r\nAUTHINFO PASS pass\r\n"
    assert (client.username, client.password) == ("user", "pass")


def test_login_uses_constructor_credentials(connect: Connect) -> None:
    server = WELCOME + MODE_READER + b"281 ok\r\n"
    client, sock = connect(server, username="fred")
    sock.sent = b""
    client.login()
    assert sock.sent == b"AUTHINFO USER fred\r\n"


def test_login_without_username(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match="No username"):
        client.login()
    assert sock.sent == b""


def test_login_rejected(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"381 more\r\n" + b"481 no\r\n")
    with pytest.raises(nntp.NNTPTemporaryError) as info:
        client.login("user", "bad")
    assert info.value.code == 481


@pytest.mark.parametrize("call", ["head", "next"])
def test_unexpected_multiline_reply_is_read(connect: Connect, call: str) -> None:
    # A 222 data line that looks like a DATE reply must not be read as one
    server = b"222 1 <a@b>\r\n111 19990101000000\r\n.\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPReplyError) as info:
        getattr(client, call)()
    assert info.value.code == 222
    assert client.date().year == 2024


def test_unexpected_multiline_final_post_reply_is_read(connect: Connect) -> None:
    server = b"340 send\r\n" + b"220 0 <a@b>\r\n111 19990101000000\r\n.\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPReplyError) as info:
        client.post({"Subject": "s"}, b"x\n")
    assert info.value.code == 220
    assert client.date().year == 2024


def test_480_to_authinfo_does_not_recurse(connect: Connect) -> None:
    server = WELCOME + MODE_READER + b"480 again\r\n" * 2 + DATE
    client, sock = connect(server, username="u", password="p")
    sock.sent = b""
    with pytest.raises(nntp.NNTPTemporaryError):
        client.date()
    assert sock.sent == b"DATE\r\nAUTHINFO USER u\r\n"
    assert client.date().year == 2024


def test_mode_reader_480_is_raised(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"480 auth\r\n", username="u")
    sock.sent = b""
    with pytest.raises(nntp.NNTPTemporaryError):
        client.command("mode  reader")  # Any spelling; login() is the caller's call
    assert sock.sent == b"mode  reader\r\n"


@pytest.mark.parametrize(("verb", "args"), [(" STARTTLS ", None), ("", "starttls")])
def test_raw_starttls_480_never_logs_in(
    connect: Connect, verb: str, args: str | None
) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"480 auth\r\n", username="u")
    sock.sent = b""
    with pytest.raises(nntp.NNTPTemporaryError, match="before STARTTLS"):
        client.command(verb, args)
    assert b"AUTHINFO" not in sock.sent


def test_command_args_surrogateescape(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"211 1 1 1 caf\xe9\r\n")
    sock.sent = b""
    client.group("caf\udce9")  # A name as decoded from LIST ACTIVE
    assert sock.sent == b"GROUP caf\xe9\r\n"


@pytest.mark.parametrize(("user", "password"), [("u", "a\nb"), ("u\r", "p")])
def test_login_bad_credentials_sends_nothing(
    connect: Connect, user: str, password: str
) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match="CR, LF or NUL"):
        client.login(user, password)
    assert sock.sent == b""


def test_login_bad_credentials_not_kept(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER, username="fred", password="pw")
    with pytest.raises(ValueError, match="CR, LF or NUL"):
        client.login("bad\n", "x")
    assert (client.username, client.password) == ("fred", "pw")


def test_login_password_asked_but_none(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"381 more\r\n")
    sock.sent = b""
    with pytest.raises(nntp.NNTPReplyError, match="no password") as info:
        client.login("u")
    assert info.value.code == 381
    assert sock.sent == b"AUTHINFO USER u\r\n"  # No bare AUTHINFO PASS


@pytest.mark.parametrize("error", [b"400 cannot quit\r\n", b""])
def test_exit_keeps_the_with_block_exception(connect: Connect, error: bytes) -> None:
    client, sock = connect(WELCOME + MODE_READER + error)
    with pytest.raises(KeyError), client:
        raise KeyError("mine")
    assert sock.closed


def test_480_logs_in_and_retries(connect: Connect) -> None:
    server = WELCOME + MODE_READER + b"480 auth\r\n381 more\r\n281 ok\r\n" + DATE
    client, sock = connect(server, username="u", password="p")
    sock.sent = b""
    assert client.date().year == 2024
    assert sock.sent == b"DATE\r\nAUTHINFO USER u\r\nAUTHINFO PASS p\r\nDATE\r\n"


@pytest.mark.parametrize(
    ("prefix", "sent", "call"),
    [
        (b"", b"CAPABILITIES", lambda c: c.capabilities()),
        (b"", b"NEWGROUPS ", lambda c: c.newgroups(datetime.now(UTC))),
        (b"", b"NEWNEWS ", lambda c: c.newnews("*", datetime.now(UTC))),
        (b"", b"LIST ACTIVE", lambda c: c.list("ACTIVE")),
        (b"", b"LIST ACTIVE", lambda c: c.list_active()),
        (b"", b"LIST ACTIVE.TIMES", lambda c: c.list_active_times()),
        (b"", b"LIST HEADERS", lambda c: c.list_headers()),
        (b"", b"LIST NEWSGROUPS", lambda c: c.list_newsgroups()),
        (b"", b"LIST OVERVIEW.FMT", lambda c: c.list_overview_fmt()),
        (b"", b"LIST EXTENSIONS", lambda c: c.list_extensions()),
        (b"", b"LISTGROUP g", lambda c: c.listgroup("g")),
        (b"", b"HDR Subject", lambda c: c.hdr("Subject")),
        (b"", b"XHDR Subject", lambda c: c.xhdr("Subject")),
        (b"", b"XZHDR Subject", lambda c: c.xzhdr("Subject")),
        (OVERVIEW_FMT, b"XOVER", lambda c: c.xover()),
        (OVERVIEW_FMT, b"XZVER", lambda c: c.xzver()),
        (OVERVIEW_FMT, b"OVER", lambda c: c.over()),
        (b"", b"XPAT Subject 1 *", lambda c: c.xpat("Subject", 1, "*")),
    ],
)
def test_iterator_methods_send_when_called(
    connect: Connect,
    prefix: bytes,
    sent: bytes,
    call: Callable[[nntp.NNTPClient], object],
) -> None:
    client, sock = connect(WELCOME + MODE_READER + prefix + b"502 no\r\n" + DATE)
    if prefix:
        client.overview_fmt  # noqa: B018 - fetch the format first
    sock.sent = b""
    with pytest.raises(nntp.NNTPPermanentError):
        call(client)  # Raised by the call, not on first next()
    assert sock.sent.startswith(sent)
    assert client.date().year == 2024


def test_iterator_abandoned_before_reconnect(connect: Connect) -> None:
    listing = b"215 list\r\nalt.test 2 1 y\r\nalt.more 2 1 y\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + listing, WELCOME + MODE_READER + DATE)
    groups = client.list_active()
    next(groups)
    client.reconnect()
    del groups  # Closing the old iterator must not break the new connection
    gc.collect()
    assert client.date().year == 2024


@pytest.mark.parametrize("started", [True, False])
def test_iterator_resumed_after_reconnect_raises(
    connect: Connect, started: bool
) -> None:
    listing = b"215 list\r\nalt.test 2 1 y\r\nalt.more 2 1 y\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + listing, WELCOME + MODE_READER + DATE)
    groups = client.list_active()
    if started:
        next(groups)
    client.reconnect()
    with pytest.raises(NNTPSyncError, match="after reconnect"):
        next(groups)  # Must not read the new connection's lines
    assert client.date().year == 2024  # The new connection is untouched


@pytest.mark.parametrize("started", [True, False])
@pytest.mark.parametrize("leave", ["close", "exit"])
def test_iterator_resumed_after_close_raises(
    connect: Connect, started: bool, leave: str
) -> None:
    listing = b"215 list\r\nalt.test 2 1 y\r\nalt.more 2 1 y\r\n.\r\n"
    client, sock = connect(WELCOME + MODE_READER + listing)
    with client if leave == "exit" else closing(client):
        groups = client.list_active()
        if started:
            next(groups)
    assert sock.closed
    with pytest.raises(NNTPSyncError, match="or close"):
        next(groups)  # Must not yield the lines still buffered


def test_login_unexpected_multiline_reply_is_read(connect: Connect) -> None:
    server = b"222 1 <a@b>\r\n111 19990101000000\r\n.\r\n" + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPReplyError) as info:
        client.login("u", "p")
    assert info.value.code == 222
    assert client.date().year == 2024  # Not 1999 from the unread block


@pytest.mark.parametrize(
    ("username", "password"), [("u" * 600, "p"), ("u", "S3cret" + "p" * 600)]
)
def test_login_too_long_sends_nothing(
    connect: Connect, username: str, password: str
) -> None:
    """A credential too long for its command raises before anything is sent,
    and the error message does not contain it."""
    client, sock = connect(WELCOME + MODE_READER + DATE)
    sock.sent = b""
    with pytest.raises(ValueError, match="longer than 512") as info:
        client.login(username, password)
    assert sock.sent == b""
    assert "u" * 600 not in str(info.value)
    assert "S3cret" not in str(info.value)
    assert client.date().year == 2024


def test_480_login_too_long_sends_nothing(connect: Connect) -> None:
    """The automatic login after a 480 checks the length before AUTHINFO USER."""
    client, sock = connect(
        WELCOME + MODE_READER + b"480 auth\r\n", username="u", password="S3cret" * 100
    )
    sock.sent = b""
    with pytest.raises(ValueError, match="longer than 512") as info:
        client.date()
    assert sock.sent == b"DATE\r\n"
    assert "S3cret" not in str(info.value)


@pytest.mark.parametrize(
    ("verb", "args"),
    [("AUTHINFO PASS", "S3cret" + "p" * 600), ("AUTHINFO PASS", "S3cret\r\nQUIT")],
)
def test_raw_authinfo_errors_hide_credentials(
    connect: Connect, verb: str, args: str
) -> None:
    client, sock = connect(WELCOME + MODE_READER)
    sock.sent = b""
    with pytest.raises(ValueError, match=r"AUTHINFO PASS \.\.\.") as info:
        client.command(verb, args)
    assert "S3cret" not in str(info.value)
    assert "AUTHINFO PASS ..." in str(info.value)
    assert sock.sent == b""


def test_480_after_login_is_raised(connect: Connect) -> None:
    """A second 480 after logging in is raised instead of logging in again."""
    server = WELCOME + MODE_READER + b"480 auth\r\n381 more\r\n281 ok\r\n480 again\r\n"
    client, sock = connect(server + DATE, username="u", password="p")
    sock.sent = b""
    with pytest.raises(nntp.NNTPTemporaryError, match="after logging in") as info:
        client.date()
    assert info.value.code == 480
    assert sock.sent == b"DATE\r\nAUTHINFO USER u\r\nAUTHINFO PASS p\r\nDATE\r\n"
    assert client.date().year == 2024  # The status line was read; in sync


def test_default_tls_context_requires_tls12(
    connect: Connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ssl.SSLContext] = []
    default_context = ssl.create_default_context

    def capture() -> ssl.SSLContext:
        context = default_context()
        # As with an OpenSSL configuration that allows TLS 1.0 and 1.1
        context.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        contexts.append(context)
        return context

    monkeypatch.setattr("nntp.nntp.ssl.create_default_context", capture)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", lambda _, sock, **__: sock)
    connect(WELCOME + MODE_READER, use_ssl=True)
    assert [c.minimum_version for c in contexts] == [ssl.TLSVersion.TLSv1_2]


def test_given_tls_context_is_not_changed(
    connect: Connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    minimum = context.minimum_version
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", lambda _, sock, **__: sock)
    connect(WELCOME + MODE_READER, use_ssl=True, ssl_context=context)
    assert context.minimum_version == minimum
