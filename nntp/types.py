from typing import NamedTuple

Range = int | tuple[int] | tuple[int, int]


class Newsgroup(NamedTuple):
    name: str
    low: int
    high: int
    status: str
