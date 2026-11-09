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

from collections.abc import Iterable, Iterator, Mapping, MutableMapping

__all__ = ["HeaderDict"]


class HeaderDict(MutableMapping[str, str]):
    """Mapping of header names to values, with case-insensitive names.

    Names keep the case they were first set with and iterate in insertion
    order. Equality with another mapping ignores name case and order.
    """

    __hash__ = None  # type: ignore[assignment]  # Mutable, so not hashable

    def __init__(
        self,
        other: Mapping[str, str] | Iterable[tuple[str, str]] = (),
        **kwargs: str,
    ) -> None:
        self._items: dict[str, tuple[str, str]] = {}  # casefold(name) -> item
        self.update(other, **kwargs)

    @classmethod
    def _from_fields(
        cls, keys: tuple[str, ...], names: tuple[str, ...], values: Iterable[str]
    ) -> HeaderDict:
        """Build from casefolded keys and names computed once by the caller.

        Used for XOVER, where every line has the same unique field names; this
        skips the per-item work of __setitem__.
        """
        self = cls.__new__(cls)
        # A line may have fewer values than names; missing fields are left out.
        self._items = dict(zip(keys, zip(names, values, strict=False), strict=False))
        return self

    def __getitem__(self, key: str) -> str:
        return self._items[key.casefold()][1]

    def __setitem__(self, key: str, value: str) -> None:
        folded = key.casefold()
        item = self._items.get(folded)
        self._items[folded] = (key if item is None else item[0], value)

    def __delitem__(self, key: str) -> None:
        del self._items[key.casefold()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.casefold() in self._items

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def _folded(self) -> dict[str, str]:
        return {folded: value for folded, (_, value) in self._items.items()}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return NotImplemented
        if not isinstance(other, HeaderDict):
            if not all(isinstance(k, str) for k in other):
                return False
            other = HeaderDict(other)
        return self._folded() == other._folded()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self._items.values())!r})"
