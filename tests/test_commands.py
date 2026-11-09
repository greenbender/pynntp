"""Group, listing and lookup commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

import nntp
from nntp.headerdict import HeaderDict
from nntp.types import Newsgroup, SSLMode
from tests.helpers import LIST, MODE_READER, WELCOME, Connect

if TYPE_CHECKING:
    from collections.abc import Callable


def test_group(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"211 3 10 12 alt.test\r\n")
    assert client.group("alt.test") == (3, 10, 12, "alt.test")
    assert sock.sent.endswith(b"GROUP alt.test\r\n")


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


@pytest.mark.parametrize("verb", ["next", "last"])
def test_next_last(connect: Connect, verb: str) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"223 7 <a@b> selected\r\n")
    assert getattr(client, verb)() == (7, "<a@b>")
    assert sock.sent.endswith(verb.upper().encode() + b"\r\n")


def test_next_bad_status(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"223 x\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid NEXT status"):
        client.next()


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


def test_sync_error_is_exported() -> None:
    assert nntp.NNTPSyncError is nntp.nntp.NNTPSyncError


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


@pytest.mark.parametrize(
    "reply", [b"223 -5 <a@b>", b"223 3", b"223 3 nothing", b"223 3 <no-at>"]
)
def test_stat_invalid_metadata(connect: Connect, reply: bytes) -> None:
    client, _ = connect(WELCOME + MODE_READER + reply + b"\r\n")
    with pytest.raises(nntp.NNTPDataError, match="Invalid STAT status"):
        client.stat(1)


def test_stat_by_message_id_number_zero(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"223 0 <a@b>\r\n")
    assert client.stat("<a@b>") == (0, "<a@b>")


@pytest.mark.parametrize("number", [b"0", b"-1"])
def test_listgroup_non_positive_number(connect: Connect, number: bytes) -> None:
    server = b"211 1 1 1 g\r\n" + number + b"\r\n.\r\n"
    client, _ = connect(WELCOME + MODE_READER + server)
    with pytest.raises(nntp.NNTPDataError, match="Invalid LISTGROUP line"):
        list(client.listgroup("g"))


def test_xhdr_by_message_id_legacy_rows(connect: Connect) -> None:
    client, _ = connect(WELCOME + MODE_READER + b"221 ok\r\n<a@b> subj\r\n.\r\n")
    assert list(client.xhdr("Subject", "<a@b>")) == [(0, "subj")]


def test_newgroups_year_zero_padded(connect: Connect) -> None:
    client, sock = connect(WELCOME + MODE_READER + b"231 ok\r\n.\r\n")
    sock.sent = b""
    assert list(client.newgroups(datetime(999, 1, 2, 3, 4, 5, tzinfo=UTC))) == []
    assert sock.sent == b"NEWGROUPS 09990102 030405 GMT\r\n"
