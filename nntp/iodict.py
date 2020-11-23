"""
Case-insentitive ordered dictionary (useful for headers).
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


from collections import OrderedDict, namedtuple
try:
    from collections.abc import MutableMapping, Mapping
except ImportError:
    from collections import MutableMapping, Mapping
from .polyfill import cached_property


__all__ = ['IODict']


class IKey(object):

    def __init__(self, orig):
        self.orig = orig

    @classmethod
    def _uncase(cls, value):
        if hasattr(value, 'casefold'):
            return value.casefold()
        if hasattr(value, 'lower'):
            return value.lower()
        if isinstance(value, tuple):
            return tuple(cls._uncase(v) for v in value)
        return value

    @cached_property
    def value(self):
        return self._uncase(self.orig)

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        if not isinstance(other, IKey):
            return self == IKey(other)
        return self.value == other.value

    def __repr__(self):
        return repr(self.orig)

    def __str__(self):
        return str(self.orig)


class IODict(MutableMapping):
    """Case in-sensitive ordered dictionary.
    >>> iod = IODict([('ABC', 1), ('DeF', 'A'), (('gHi', 'jkl', 20), 'b')])
    >>> iod['ABC'], iod['abc'], iod['aBc']
    (1, 1, 1)
    >>> iod['DeF'], iod['def'], iod['dEf']
    ('A', 'A', 'A')
    >>> iod[('gHi', 'jkl', 20)], iod[('ghi', 'jKL', 20)]
    ('b', 'b')
    >>> iod == {'aBc': 1, 'deF': 'A', ('Ghi', 'JKL', 20): 'b'}
    True
    >>> iod.popitem()
    (('gHi', 'jkl', 20), 'b')
    """

    def __init__(self, *args, **kwargs):
        self.__proxy = OrderedDict()
        for arg in args:
            self.update(arg)
        self.update(kwargs)

    def __getitem__(self, key):
        return self.__proxy[IKey(key)]

    def __setitem__(self, key, value):
        self.__proxy[IKey(key)] = value

    def __delitem__(self, key):
        del self.__proxy[IKey(key)]

    def __iter__(self):
        for key in self.__proxy:
            yield key.orig

    def __len__(self):
        return len(self.__proxy)

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        if not isinstance(other, IODict):
            return self == IODict(other)
        return self.__proxy == other.__proxy

    def __repr__(self):
        clsname = type(self).__name__
        return '%s(%r)' % (clsname, list(self.__proxy.items()))

    def keys(self):
        for key in self.__proxy:
            yield key.orig

    def items(self):
        for key in self.__proxy:
            yield key.orig, self[key.orig]

    def popitem(self):
        key, value = self.__proxy.popitem()
        return key.orig, value


if __name__ == "__main__":
    import doctest
    doctest.testmod()
