#!/usr/bin/python

import datetime

class _gmt(datetime.tzinfo):
    """GMT timezone
    """

    def utcoffset(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "GMT"

    def dst(self, dt):
        return datetime.timedelta(0)

class _utc(_gmt):
    """UTC timezone
    """

    def tzname(self, dt):
        return "UTC"

GMT = _gmt()
UTC = _utc()

if __name__ == "__main__":
    t1 = datetime.datetime.now()
    t2 = datetime.datetime.now(UTC)
    t3 = datetime.datetime.now(GMT)
    print t1.strftime("%Y-%m-%d %H:%M:%S %Z")
    print t2.strftime("%Y-%m-%d %H:%M:%S %Z")
    print t3.strftime("%Y-%m-%d %H:%M:%S %Z")
