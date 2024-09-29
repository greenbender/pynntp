from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from typing import TYPE_CHECKING

import pytest

from nntp.utils import (
    parse_date,
    parse_epoch,
    parse_headers,
    parse_newsgroup,
    unparse_msgid_range,
    unparse_range,
)

if TYPE_CHECKING:
    from nntp.types import Newsgroup, Range


@pytest.mark.parametrize(
    ("range", "expected"),
    [
        ((1, 10), "1-10"),
        ((100,), "100-"),
        pytest.param(None, None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param((), None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param((1, 10, 20), None, marks=pytest.mark.xfail(raises=ValueError)),
    ],
)
def test_unparse_range(range: Range, expected: str) -> None:  # noqa: A002
    assert unparse_range(range) == expected


@pytest.mark.parametrize(
    ("msgid_range", "expected"),
    [
        ("msgid1", "msgid1"),
        ((1, 10), "1-10"),
        ((100,), "100-"),
        pytest.param(None, None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param((), None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param((1, 10, 20), None, marks=pytest.mark.xfail(raises=ValueError)),
    ],
)
def test_unparse_msgid_range(msgid_range: str | Range, expected: str) -> None:
    assert unparse_msgid_range(msgid_range) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("local.test 0 1 y", ("local.test", 0, 1, "y")),
        ("local.test 0 1 n", ("local.test", 0, 1, "n")),
        ("alt.test 10 20 y", ("alt.test", 10, 20, "y")),
        ("alt.test\t10\t20 ?", ("alt.test", 10, 20, "?")),
        pytest.param("alt.test", None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param("alt.test 10", None, marks=pytest.mark.xfail(raises=ValueError)),
        pytest.param(
            "alt.test 10 20", None, marks=pytest.mark.xfail(raises=ValueError)
        ),
    ],
)
def test_parse_newsgroup(line: str, expected: Newsgroup) -> None:
    assert parse_newsgroup(line) == expected


def test_parse_headers() -> None:
    headers = [
        "Subject: Test Subject",
        "From: John Doe <johndoe@example.com>",
        "Date: Mon, 01 Jan 2022 12:00:00 GMT",
        "Message-ID: <1234567890@example.com>",
    ]
    expected = {
        "Subject": "Test Subject",
        "FroM": "John Doe <johndoe@example.com>",
        "DATE": "Mon, 01 Jan 2022 12:00:00 GMT",
        "message-id": "<1234567890@example.com>",
    }
    assert parse_headers(headers) == expected
    assert parse_headers("\r\n".join(headers)) == expected
    assert parse_headers(StringIO("\r\n".join(headers))) == expected


def test_parse_headers_continuation() -> None:
    headers = [
        "Subject: Test Subject",
        " with continuation",
        "X-Items: Apple",
        "\tBanana",
        "\tCarrot",
    ]
    expected = {
        "Subject": "Test Subject with continuation",
        "X-Items": "Apple\tBanana\tCarrot",
    }
    assert parse_headers(headers) == expected


def test_parse_headers_invalid() -> None:
    with pytest.raises(ValueError, match="First header is a continuation"):
        parse_headers(" Subject: Test Subject")
    with pytest.raises(ValueError, match="First header is a continuation"):
        parse_headers("\twith continuation")
    with pytest.raises(ValueError, match="not enough values to unpack"):
        parse_headers("Invalid header")


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        ("20220101144001", datetime(2022, 1, 1, 14, 40, 1, tzinfo=timezone.utc)),
        (20220101144001, datetime(2022, 1, 1, 14, 40, 1, tzinfo=timezone.utc)),
        pytest.param("2022", None, marks=pytest.mark.xfail(raises=ValueError)),
    ],
)
def test_parse_date(date: str | int, expected: datetime) -> None:
    assert parse_date(date) == expected


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        ("1641048001", datetime(2022, 1, 1, 14, 40, 1, tzinfo=timezone.utc)),
        (1641048001, datetime(2022, 1, 1, 14, 40, 1, tzinfo=timezone.utc)),
    ],
)
def test_parse_epoch(epoch: str | int, expected: datetime) -> None:
    assert parse_epoch(epoch) == expected
