"""
Parsing and formatting helpers for NNTP arguments and responses.
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
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING

from .headerdict import HeaderDict
from .types import Newsgroup, Range

# RFC 5322 field name: printable ASCII except ":".
_HEADER_NAME_RE = re.compile(r"[!-9;-~]+")
# A folded header value continues after a line break followed by whitespace.
_HEADER_FOLD_RE = re.compile(r"\r?\n(?=[ \t])")

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def unparse_range(obj: Range) -> str:
    """Format an article range as a command argument.

    Args:
        obj: An article number, (first,) for first onwards, or (first, last).

    Returns:
        The range, e.g. "4678", "4245-" or "4245-5234".

    Raises:
        ValueError: If obj is not one of those forms.
    """
    if isinstance(obj, int) and not isinstance(obj, bool):
        return str(obj)

    if isinstance(obj, tuple):
        if any(isinstance(bound, bool) for bound in obj):
            raise ValueError("Invalid range format")
        if len(obj) == 1:
            return f"{obj[0]}-"
        if len(obj) == 2:
            return f"{obj[0]}-{obj[1]}"
        raise ValueError("Invalid range format")

    raise ValueError("Must be an integer or tuple")


def unparse_msgid_range(obj: str | Range) -> str:
    """Format a message-id or article range as a command argument.

    Args:
        obj: A message-id, or a range as for unparse_range().

    Returns:
        The message-id unchanged, or the formatted range.

    Raises:
        ValueError: If obj is not a valid range.
    """
    if isinstance(obj, str):
        return obj

    return unparse_range(obj)


def parse_newsgroup(line: str) -> Newsgroup:
    """Parse a LIST ACTIVE or NEWGROUPS line.

    The fields are name, high water mark, low water mark and status (RFC 3977
    section 7.6.3).

    Args:
        line: The response line.

    Returns:
        The newsgroup.

    Raises:
        ValueError: If the line cannot be parsed.
    """
    parts = line.split()
    try:
        name = parts[0]
        high = int(parts[1])
        low = int(parts[2])
        status = parts[3]
    except (IndexError, ValueError) as e:
        raise ValueError("Invalid newsgroup info") from e
    return Newsgroup(name, low, high, status)


def parse_active_time(line: str) -> tuple[str, datetime, str]:
    """Parse a LIST ACTIVE.TIMES line.

    Args:
        line: The response line: name, creation time (seconds since the
            epoch) and creator.

    Returns:
        The newsgroup name, its creation time in UTC and its creator.

    Raises:
        ValueError: If the line cannot be parsed.
    """
    name, epoch, creator, *_ = line.split()
    return name, parse_epoch(epoch), creator


def parse_newsgroup_description(line: str) -> tuple[str, str]:
    """Parse a LIST NEWSGROUPS line.

    Args:
        line: The response line: name, then an optional description.

    Returns:
        The newsgroup name and description ("" if there is none).

    Raises:
        ValueError: If the line is empty.
    """
    name, *description = line.strip().split(None, 1)
    return name, description[0] if description else ""


def parse_overview_fmt(line: str) -> tuple[str, bool]:
    """Parse a LIST OVERVIEW.FMT line (RFC 3977 section 8.4).

    Args:
        line: The response line, e.g. "Subject:", ":bytes" or "Xref:full".

    Returns:
        The field name and whether the field is "full" (its values include
        the header name).

    Raises:
        ValueError: If the line cannot be parsed.
    """
    name, suffix = line.rstrip().split(":")
    if suffix and not name:  # Metadata such as ":bytes"
        name, suffix = suffix, name
    full = suffix.lower() == "full"  # ABNF strings ignore case
    if suffix and not full:
        raise ValueError(f"unknown suffix {suffix!r}")
    return name, full


def parse_number_value(line: str) -> tuple[int, str]:
    """Parse an HDR, XHDR or XPAT line: an article number, then a value.

    A line starting with a message-id (older XHDR by message-id) gets
    number 0, as HDR gives.

    Args:
        line: The response line.

    Returns:
        The article number and the value ("" if there is none).

    Raises:
        ValueError: If the line cannot be parsed.
    """
    first, *value = line.rstrip("\r\n").split(None, 1)
    number = 0 if first.startswith("<") else int(first)
    return number, value[0] if value else ""


def parse_headers(obj: str | Iterable[str]) -> HeaderDict:
    """Parse header lines up to the first blank line.

    Continuation lines are unfolded. CRLF and LF line endings are accepted.

    Args:
        obj: A string, or an iterable of lines such as a file.

    Returns:
        The headers. If a header is repeated the last value wins.

    Raises:
        ValueError: If the first line is a continuation line or a line is not
            a header.
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
    """Format headers as an article header block, ending with a blank line.

    Values may be folded (RFC 5322 section 2.2.3): a line break followed by a
    space or tab. LF line breaks in folds are sent as CRLF.

    Args:
        hdrs: The headers.

    Returns:
        The header block.

    Raises:
        ValueError: If a name is not printable ASCII without ":", or a value
            contains NUL or a CR/LF that is not part of a fold. Either would
            let the header end early and inject extra headers or body text.
    """
    lines = []
    for name, value in hdrs.items():
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid header name: {name!r}")
        if any(c in _HEADER_FOLD_RE.sub("", value) for c in "\r\n\0"):
            raise ValueError(f"Invalid header value for {name}: {value!r}")
        value = _HEADER_FOLD_RE.sub("\r\n", value)
        lines.append(f"{name}: {value}\r\n")
    return "".join(lines) + "\r\n"


def parse_date(value: str | int) -> datetime:
    """Parse a date as returned by the DATE command.

    Args:
        value: A date in the format YYYYMMDDHHMMSS.

    Returns:
        The date, in UTC.

    Raises:
        ValueError: If the value is not a valid date in that format.
    """
    text = str(value)
    if len(text) != 14 or not (text.isascii() and text.isdigit()):
        raise ValueError(f"Invalid date: {value!r}")
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def parse_epoch(value: str | int) -> datetime:
    """Parse a Unix timestamp, as used by LIST ACTIVE.TIMES.

    Args:
        value: Seconds since the epoch.

    Returns:
        The time, in UTC.

    Raises:
        ValueError: If the value is not an integer, or is out of range.
    """
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError) as e:  # Too large for datetime or the OS
        raise ValueError(f"Timestamp out of range: {value!r}") from e
