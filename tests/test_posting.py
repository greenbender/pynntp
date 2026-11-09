"""POST and IHAVE."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import nntp
from nntp.nntp import NNTPSyncError
from tests.helpers import DATE, IHAVE_OK, MODE_READER, ODD, WELCOME, Connect

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_post(connect: Connect) -> None:
    client, sock = connect(
        WELCOME + MODE_READER + b"340 send\r\n" + b"240 <new@id> posted\r\n"
    )
    sock.sent = b""
    assert client.post({"Subject": "hi"}, b"one\n.two\n") == "<new@id>"
    assert sock.sent == b"POST\r\nSubject: hi\r\n\r\none\r\n..two\r\n.\r\n"


def test_post_header_surrogateescape(connect: Connect) -> None:
    client, sock = connect(
        WELCOME + MODE_READER + b"340 send\r\n" + b"240 <new@id> posted\r\n"
    )
    sock.sent = b""
    client.post({"Subject": "caf\udce9"}, b"x\n")  # As read with surrogateescape
    assert b"Subject: caf\xe9\r\n" in sock.sent


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        (b"240 Article received <new@id>", "<new@id>"),  # INN
        (b"240 <new@id> posted", "<new@id>"),
        (b"240 article posted ok", True),
        (b"240", True),
    ],
)
def test_post_message_id_from_reply(
    connect: Connect, reply: bytes, result: str | bool
) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"340 send\r\n" + reply + b"\r\n")
    assert client.post({"Subject": "hi"}, b"x\n") == result


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


def test_post_unexpected_final_reply(connect: Connect) -> None:
    server = b"340 send\r\n" + ODD + DATE
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPReplyError) as info:
        client.post({"Subject": "x"}, b"x\n")
    assert info.value.code == 299
    assert client.date().year == 2024
