from enum import Enum
from typing import NamedTuple, Union

Range = Union[int, tuple[int], tuple[int, int]]


class Newsgroup(NamedTuple):
    name: str
    low: int
    high: int
    status: str


class SSLMode(str, Enum):
    IMPLICIT = "implicit"
    """Establish secure connection immediately.
    You need to use a different port (usually 563) in this mode.
    """

    STARTTLS = "starttls"
    """Establish secure connection dynamically after sending a `STARTTLS` command.
    This mode is not recommended. See <https://www.rfc-editor.org/rfc/rfc8143.html#section-2>
    You can use the same port (usually 119) this mode.
    """
