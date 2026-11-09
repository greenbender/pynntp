from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING

import pytest

from nntp.types import Newsgroup
from nntp.utils import (
    parse_date,
    parse_epoch,
    parse_headers,
    parse_newsgroup,
    unparse_headers,
    unparse_msgid_range,
    unparse_range,
)

if TYPE_CHECKING:
    from nntp.types import Range


@pytest.mark.parametrize(
    ("range", "expected"),
    [(1234, "1234"), ((1, 10), "1-10"), ((100,), "100-")],
)
def test_unparse_range(range: Range, expected: str) -> None:  # noqa: A002
    assert unparse_range(range) == expected
    assert unparse_msgid_range(range) == expected


@pytest.mark.parametrize("range", [None, (), (1, 10, 20), "1-10", True])
def test_unparse_range_invalid(range: object) -> None:  # noqa: A002
    with pytest.raises(ValueError):  # noqa: PT011
        unparse_range(range)  # type: ignore[arg-type]


def test_unparse_msgid_range() -> None:
    assert unparse_msgid_range("<msgid@example.com>") == "<msgid@example.com>"
    with pytest.raises(ValueError):  # noqa: PT011
        unparse_msgid_range(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # RFC 3977 section 7.6.3 example: name, high, low, status.
        ("misc.test 3002322 3000234 y", Newsgroup("misc.test", 3000234, 3002322, "y")),
        # INN reports an empty group as high 0, low 1.
        ("junk 0000000000 0000000001 n", Newsgroup("junk", 1, 0, "n")),
        ("alt.test\t20\t10 m\r\n", Newsgroup("alt.test", 10, 20, "m")),
    ],
)
def test_parse_newsgroup(line: str, expected: Newsgroup) -> None:
    assert parse_newsgroup(line) == expected


@pytest.mark.parametrize(
    "line", ["alt.test", "alt.test 10", "alt.test 10 20", "alt.test x 1 y"]
)
def test_parse_newsgroup_invalid(line: str) -> None:
    with pytest.raises(ValueError, match="Invalid newsgroup info"):
        parse_newsgroup(line)


HEADERS = [
    "Subject: Test Subject",
    "From: John Doe <johndoe@example.com>",
    "Date: Mon, 01 Jan 2022 12:00:00 GMT",
    "Message-ID: <1234567890@example.com>",
]
EXPECTED = {
    "Subject": "Test Subject",
    "FroM": "John Doe <johndoe@example.com>",
    "DATE": "Mon, 01 Jan 2022 12:00:00 GMT",
    "message-id": "<1234567890@example.com>",
}


@pytest.mark.parametrize("eol", ["\r\n", "\n"])
def test_parse_headers(eol: str) -> None:
    text = eol.join(HEADERS)
    assert parse_headers(HEADERS) == EXPECTED
    assert parse_headers(text) == EXPECTED
    assert parse_headers(StringIO(text)) == EXPECTED


@pytest.mark.parametrize("eol", ["\r\n", "\n"])
def test_parse_headers_stops_at_blank_line(eol: str) -> None:
    lines = iter([f"Subject: x{eol}", eol, f"Not: a header{eol}", f"body{eol}"])
    assert parse_headers(lines) == {"Subject": "x"}
    assert next(lines) == f"Not: a header{eol}"  # The body is left unread


def test_parse_headers_continuation() -> None:
    headers = [
        "Subject: Test Subject\r\n",
        " with continuation\r\n",
        "X-Items: Apple\r\n",
        "\tBanana\r\n",
        "\tCarrot\r\n",
    ]
    expected = {
        "Subject": "Test Subject with continuation",
        "X-Items": "Apple\tBanana\tCarrot",
    }
    assert parse_headers(headers) == expected


def test_parse_headers_value_with_colon() -> None:
    assert parse_headers(["Date: 12:00:00\r\n"]) == {"Date": "12:00:00"}


def test_parse_headers_repeated_last_wins() -> None:
    assert parse_headers(["A: 1\r\n", "a: 2\r\n"]) == {"A": "2"}


def test_parse_headers_invalid() -> None:
    with pytest.raises(ValueError, match="First header is a continuation"):
        parse_headers(" Subject: Test Subject")
    with pytest.raises(ValueError, match="First header is a continuation"):
        parse_headers("\twith continuation")
    with pytest.raises(ValueError, match="Invalid header line"):
        parse_headers("Invalid header")


def test_unparse_headers() -> None:
    assert unparse_headers({"Subject": "hi", "From": "me"}) == (
        "Subject: hi\r\nFrom: me\r\n\r\n"
    )
    assert unparse_headers({}) == "\r\n"
    assert parse_headers(unparse_headers(EXPECTED)) == EXPECTED


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ("one\r\n two", "one\r\n two"),
        ("one\r\n\ttwo\r\n three", "one\r\n\ttwo\r\n three"),
        ("one\n two", "one\r\n two"),  # LF fold sent as CRLF
    ],
)
def test_unparse_headers_folded(value: str, wire: str) -> None:
    text = unparse_headers({"X-Long": value})
    assert text == f"X-Long: {wire}\r\n\r\n"
    assert parse_headers(text) == {"X-Long": wire.replace("\r\n", "")}


@pytest.mark.parametrize(
    "headers",
    [
        {"Subject": "hi\r\nNewsgroups: alt.spam"},
        {"Subject": "hi\nX: y"},
        {"Subject": "hi\r"},
        {"Subject": "hi\r\n"},  # Would end the headers early
        {"Subject": "hi\r \r\nX: y"},  # Bare CR is not a fold
        {"Subject": "hi\r\n \r\n"},
        {"Subject": "hi\0"},
        {"Bad Name": "x"},
        {"Bad:Name": "x"},
        {"Bad\r\nName": "x"},
        {"": "x"},
        {"Sübject": "x"},
    ],
)
def test_unparse_headers_rejects_injection(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="Invalid header"):
        unparse_headers(headers)


@pytest.mark.parametrize(
    "date",
    ["20220101144001", 20220101144001],
)
def test_parse_date(date: str | int) -> None:
    assert parse_date(date) == datetime(2022, 1, 1, 14, 40, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "date",
    [
        "2022",
        "1020220101144001",  # Too long; used to silently drop the extra digits
        "20220101144001 extra",
        "2022010114400x",
        "20221301000000",  # Month 13
        "-0220101144001",
    ],
)
def test_parse_date_invalid(date: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_date(date)


@pytest.mark.parametrize("epoch", ["1641048001", 1641048001])
def test_parse_epoch(epoch: str | int) -> None:
    assert parse_epoch(epoch) == datetime(2022, 1, 1, 14, 40, 1, tzinfo=UTC)


def test_parse_epoch_invalid() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_epoch("not a number")
