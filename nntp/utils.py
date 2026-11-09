"""
An NNTP library - a bit more useful than the nntplib one (hopefully).
Copyright (C) 2013-2020  Byron Platt

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from io import StringIO
from typing import TYPE_CHECKING

from .headerdict import HeaderDict
from .types import Newsgroup, Range

# RFC 5322 field name: printable ASCII except ":".
_HEADER_NAME_RE = re.compile(r"[!-9;-~]+")

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def unparse_range(obj: Range) -> str:
    """Unparse a range argument.

    Args:
        obj: An article range. There are a number of valid formats; an integer
            specifying a single article or a tuple specifying an article range.
            If the range doesn't specify a last article then all articles from
            the first specified article up to the current last article for the
            group are included.

    Returns:
        The range as a string that can be used by an NNTP command.

    Note: Sample valid formats.
        4678
        (4245,)
        (4245, 5234)
    """
    if isinstance(obj, int):
        return str(obj)

    if isinstance(obj, tuple):
        if len(obj) == 1:
            return f"{obj[0]}-"
        if len(obj) == 2:
            return f"{obj[0]}-{obj[1]}"
        raise ValueError("Invalid range format")

    raise ValueError("Must be an integer or tuple")


def unparse_msgid_range(obj: str | Range) -> str:
    """Unparse a message-id or range argument.

    Args:
        obj: A message id as a string or a range as specified by
            unparse_range().

    Raises:
        ValueError: If obj is not a valid message id or range format. See
            unparse_range() for valid range formats.

    Returns:
        A message id or range as a string that can be used by an NNTP command.
    """
    if isinstance(obj, str):
        return obj

    return unparse_range(obj)


def parse_newsgroup(line: str) -> Newsgroup:
    """Parse a newsgroup info line to python types.

    Args:
        line: An info response line containing newsgroup info.

    Lines are in the LIST ACTIVE / NEWGROUPS format (RFC 3977 section 7.6.3):
    name, high water mark, low water mark, status.

    Returns:
        A tuple of group name, low-water as integer, high-water as integer and
        posting status.

    Raises:
        ValueError: If the newsgroup info cannot be parsed.

    Note:
        Posting status is a character is one of (but not limited to):
            "y" posting allowed
            "n" posting not allowed
            "m" posting is moderated
    """
    parts = line.split()
    try:
        name = parts[0]
        high = int(parts[1])
        low = int(parts[2])
        status = parts[3]
    except (IndexError, ValueError):
        raise ValueError("Invalid newsgroup info")
    return Newsgroup(name, low, high, status)


def parse_headers(obj: str | Iterable[str]) -> HeaderDict:
    """Parse header lines up to the first blank line into a HeaderDict.

    Args:
        obj: An iterable object including file-like objects.

    Returns:
        An dictionary of headers. If a header is repeated then the last value
        for that header is given.

    Raises:
        ValueError: If the first line is a continuation line or the headers
            cannot be parsed.
    """
    if isinstance(obj, str):
        obj = StringIO(obj)
    hdrs: list[tuple[str, str]] = []
    for line in obj:
        # A blank line (CRLF or LF) ends the headers.
        if not line.rstrip("\r\n"):
            break
        # A line starting with whitespace continues the previous header.
        if line[0] in " \t":
            if not hdrs:
                raise ValueError("First header is a continuation")
            name, value = hdrs[-1]
            hdrs[-1] = name, value + line.rstrip()
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"Invalid header line: {line!r}")
        hdrs.append((name.strip(), value.strip()))
    return HeaderDict(hdrs)


def unparse_headers(hdrs: Mapping[str, str]) -> str:
    """Format a dictionary of headers as an article header block.

    Args:
        hdrs: A dictionary of headers.

    Returns:
        The headers as a string that can be used in an NNTP POST.

    Raises:
        ValueError: If a name is not printable ASCII without ":", or a value
            contains CR, LF or NUL. Either would let the header end early and
            inject extra headers or body text.
    """
    lines = []
    for name, value in hdrs.items():
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid header name: {name!r}")
        if any(c in value for c in "\r\n\0"):
            raise ValueError(f"Invalid header value for {name}: {value!r}")
        lines.append(f"{name}: {value}\r\n")
    return "".join(lines) + "\r\n"


def parse_date(value: str | int) -> datetime:
    """Parse a date as returned by the `DATE` command.

    Args:
        value: A date in the format `YYYYMMDDHHMMSS`.

    Returns:
        A datetime object representing the date with timezone set to UTC.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    text = str(value)
    if len(text) != 14 or not (text.isascii() and text.isdigit()):
        raise ValueError(f"Invalid date: {value!r}")
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def parse_epoch(value: str | int) -> datetime:
    """Parse a Unix timestamp, as used by `LIST ACTIVE.TIMES`.

    Args:
        value: Seconds since the epoch.

    Returns:
        A datetime object representing the date with timezone set to UTC.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
