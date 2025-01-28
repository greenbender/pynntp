[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/greenbender/pynntp/main.svg)](https://results.pre-commit.ci/latest/github/greenbender/pynntp/main)

# pynntp

Python NNTP library.

This package includes advanced NNTP features, including, compressed headers.

The most important (useful) feature of this package over other nntp libraries is
the ability to use generators to produce data. This allows for streaming download
of large responses to say an XOVER command (which can produce gigabytes of data)
and allows you to process the data at the same time is is being received.
Meaning that memory use is minimal (even for the largest responses) and that
cycles aren't being wasted waiting on a blocking read (even in a single threaded
application)

## Documentation

See https://greenbender.github.io/pynntp/

This is an area in need of improvement. At present docs are simply auto
generated via docstrings. Some more explicit documentation, including examples,
is on the TODO list.

## Example

    >>> import nntp
    >>> nntp_client = nntp.NNTPClient('usenet-host.com', 443, 'user', 'password', use_ssl=True)
    >>> nntp_client.date()
    datetime.datetime(2013, 10, 19, 6, 11, 41, tzinfo=_tzgmt())
    >>> nntp_client.xfeature_compress_gzip()
    True
    >>> nntp_client.date()
    datetime.datetime(2013, 10, 19, 6, 13, 3, tzinfo=_tzgmt())

## Supported Commands

NNTP commands that are currently supported include:

- CAPABILITIES
- MODE READER
- QUIT
- DATE
- HELP
- NEWGROUPS
- NEWNEWS
- LIST ACTIVE
- LIST ACTIVE.TIMES
- LIST NEWSGROUPS
- LIST OVERVIEW.FMT
- LIST EXTENSIONS
- GROUP
- NEXT
- LAST
- ARTICLE
- HEAD
- BODY
- POST
- XHDR
- XZHDR
- XOVER
- XZVER
- XPAT
- XFEATURE COMPRESS GZIP
- POST
