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

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from io import StringIO
from typing import Union

from .headerdict import HeaderDict
from .types import Newsgroup, Range


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


def unparse_msgid_range(obj: Union[str, Range]) -> str:
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
        low = int(parts[1])
        high = int(parts[2])
        status = parts[3]
    except (IndexError, ValueError):
        raise ValueError("Invalid newsgroup info")
    return Newsgroup(name, low, high, status)


def _parse_header(line: str) -> Union[str, tuple[str, str], None]:
    """Parse a header line.

    Args:
        line: A header line as a string.

    Returns:
        None if end of headers is found. A string giving the continuation line
        if a continuation is found. A tuple of name, value when a header line
        is found.

    Raises:
        ValueError: If the line cannot be parsed as a header.
    """
    # End of headers
    if not line or line == "\r\n":
        return None
    # Continuation line
    if line[0] in " \t":
        return line.rstrip()
    name, value = line.split(":", 1)
    return name.strip(), value.strip()


def parse_headers(obj: Union[str, Iterable[str]]) -> HeaderDict:
    """Parse a string a iterable object (including file like objects) to a
    python dictionary.

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
        hdr = _parse_header(line)
        if not hdr:
            break
        if isinstance(hdr, str):
            if not hdrs:
                raise ValueError("First header is a continuation")
            hdrs[-1] = hdrs[-1][0], hdrs[-1][1] + hdr
            continue
        hdrs.append(hdr)
    return HeaderDict(hdrs)


def _unparse_header(name: str, value: str) -> str:
    """Parse a name value tuple to a header string.

    Args:
        name: The header name.
        value: the header value.

    Returns:
        The header as a string.
    """
    return f"{name}: {value}" + "\r\n"


def unparse_headers(hdrs: Mapping[str, str]) -> str:
    """Parse a dictionary of headers to a string.

    Args:
        hdrs: A dictionary of headers.

    Returns:
        The headers as a string that can be used in an NNTP POST.
    """
    return "".join([_unparse_header(n, v) for n, v in hdrs.items()]) + "\r\n"


def parse_date(value: Union[str, int]) -> datetime:
    """Parse a date as returned by the `DATE` command.

    Args:
        value: A date as a string in the format `YYYYMMDDHHMMSS`.

    Returns:
        A datetime object representing the date with timezone set to UTC.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    i = int(value)
    M, S = divmod(i, 100)
    H, M = divmod(M, 100)
    d, H = divmod(H, 100)
    m, d = divmod(d, 100)
    Y, m = divmod(m, 100)
    return datetime(Y % 10000, m, d, H, M, S, tzinfo=timezone.utc)


def parse_epoch(value: Union[str, int]) -> datetime:
    """Parse a date as returned by the `DATE` command.

    Args:
        line: An epoch date as a string.

    Returns:
        A datetime object representing the date with timezone set to UTC.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
