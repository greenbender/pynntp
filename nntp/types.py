"""Types used in the NNTP client API."""

from enum import StrEnum
from typing import NamedTuple

# An article number, (first,) for first onwards, or (first, last).
Range = int | tuple[int] | tuple[int, int]


class Newsgroup(NamedTuple):
    """A newsgroup, as returned by LIST ACTIVE and NEWGROUPS.

    status is the posting status, usually "y" (allowed), "n" (not allowed)
    or "m" (moderated).
    """

    name: str
    low: int
    high: int
    status: str


class SSLMode(StrEnum):
    """How a TLS connection is established."""

    IMPLICIT = "implicit"
    """Establish secure connection immediately.
    You need to use a different port (usually 563) in this mode.
    """

    STARTTLS = "starttls"
    """Establish secure connection dynamically after sending a `STARTTLS` command.
    This mode is not recommended. See <https://www.rfc-editor.org/rfc/rfc8143.html#section-2>
    You can use the same port (usually 119) this mode.
    """
