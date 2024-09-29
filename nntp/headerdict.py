"""
Case-insentitive ordered mapping for headers.
Copyright (C) 2013-2024  Byron Platt

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

from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from itertools import chain

__all__ = ["HeaderDict"]


class HeaderName(str):  # noqa: SLOT000
    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __hash__(self) -> int:
        return hash(self.casefold())


class HeaderDict(MutableMapping[str, str]):
    def __init__(
        self,
        other: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        **kwargs: str,
    ) -> None:
        self.__proxy = OrderedDict[HeaderName, str]()
        other_pairs: Iterable[tuple[str, str]] = (
            ()
            if other is None
            else other.items()
            if isinstance(other, Mapping)
            else other
        )
        kwargs_pairs = kwargs.items()
        for k, v in chain(other_pairs, kwargs_pairs):
            if not isinstance(k, str):
                raise TypeError(f"Header name must be a string: {k!r}")
            if not isinstance(v, str):
                raise TypeError(f"Header value must be a string: {v!r}")
            self.__proxy[HeaderName(k)] = v

    def __getitem__(self, key: str) -> str:
        return self.__proxy[HeaderName(key)]

    def __setitem__(self, key: str, value: str) -> None:
        self.__proxy[HeaderName(key)] = value

    def __delitem__(self, key: str) -> None:
        del self.__proxy[HeaderName(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__proxy)

    def __len__(self) -> int:
        return len(self.__proxy)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HeaderDict):
            return self.__proxy == other.__proxy
        if isinstance(other, (Mapping, Iterable)):
            try:
                other = HeaderDict(other)
            except (TypeError, ValueError):
                return False
            return self == HeaderDict(other)
        return False

    def __repr__(self) -> str:
        clsname = type(self).__name__
        return f"{clsname}({list(self.__proxy.items())!r})"
