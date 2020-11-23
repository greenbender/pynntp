try:
    from functools import cached_property
except ImportError:
    class cached_property(object):

        def __init__(self, func):
            self.func = func

        def __get__(self, obj, cls=None):
            if obj is None:
                return self
            value = obj.__dict__[self.func.__name__] = self.func(obj)
            return value


__all__ = [
    'cached_property'
]
