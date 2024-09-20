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

from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from typing import Any, Iterable, Iterator, Union

__all__ = ["HeaderDict"]


class HeaderName(str):  # noqa: SLOT000
    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __hash__(self) -> int:
        return hash(self.casefold())


class HeaderDict(MutableMapping[str, str]):
    def __init__(
        self,
        other: Union[Mapping[str, str], Iterable[tuple[str, str]], None] = None,
        **kwargs: str,
    ) -> None:
        if other is None:
            self.__proxy = OrderedDict[HeaderName, str]()
        elif hasattr(other, "items"):
            self.__proxy = OrderedDict((HeaderName(k), v) for k, v in other.items())
        else:
            self.__proxy = OrderedDict((HeaderName(k), v) for k, v in other)
        self.__proxy.update((HeaderName(k), v) for k, v in kwargs.items())

    def __getitem__(self, key: str) -> str:
        return self.__proxy[HeaderName(key)]

    def __setitem__(self, key: str, value: Any) -> None:
        self.__proxy[HeaderName(key)] = value

    def __delitem__(self, key: str) -> None:
        del self.__proxy[HeaderName(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__proxy)

    def __len__(self) -> int:
        return len(self.__proxy)

    def __repr__(self) -> str:
        clsname = type(self).__name__
        return f"{clsname}({list(self.__proxy.items())!r})"
