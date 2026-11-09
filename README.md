[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/greenbender/pynntp/main.svg)](https://results.pre-commit.ci/latest/github/greenbender/pynntp/main)

# pynntp

A Python NNTP client library, built for working with large Usenet servers.

- **Streaming responses.** Commands that can return a lot of data (XOVER,
  HDR, LIST, ...) return iterators, so gigabytes of overview data can be
  processed as it arrives, with little memory.
- **Compressed headers.** Supports `XFEATURE COMPRESS GZIP` and the
  yEnc/zlib compressed XZVER and XZHDR commands, decompressed transparently.
- **yEnc.** Article bodies are yEnc-decoded and checked against their size
  and CRC.
- **TLS and authentication.** Implicit TLS or STARTTLS, and AUTHINFO login
  sent when the server asks for it.
- **No dependencies** outside the standard library.

Documentation, including the API reference:
<https://greenbender.github.io/pynntp/>

## Installation

```sh
pip install pynntp
```

or `uv add pynntp`. Requires Python 3.11 or newer. The package is imported
as `nntp`.

## Quickstart

### Connecting

```python
import nntp

with nntp.NNTPClient(
    "news.example.com", username="user", password="secret", use_ssl=True
) as client:
    print(client.date())
```

The username and password are only sent if the server asks for them, and in
clear text unless TLS is used. With `use_ssl=True` the port defaults to 563;
for STARTTLS on port 119 instead, also pass `ssl_mode=nntp.SSLMode.STARTTLS`.
TLS certificates are verified;
pass `ssl_context=` (an `ssl.SSLContext`) to trust a private CA or change other
TLS settings. Leaving the `with` block sends QUIT.

### Browsing a group

```python
total, first, last, name = client.group("alt.binaries.test")

for number, overview in client.xover((last - 999, last)):
    print(number, overview["subject"], overview["bytes"])
```

Header and overview names are case-insensitive: `overview["Subject"]` and
`overview["subject"]` are the same.

To compress the whole connection on servers that support RFC 8054, call
`client.compress()` after connecting and any login. Some servers instead
offer compressed overview data: call `client.xfeature_compress_gzip()` once,
or use `client.xzver()` instead of `xover()`.

### Downloading an article

```python
headers = client.head("<part1of10.abc@example.com>")
data = client.body("<part1of10.abc@example.com>")  # yEnc is decoded
```

A body whose yEnc data fails its size or CRC check raises `NNTPYEncError`.
The decoded data is still available, for example for par2 repair:

```python
try:
    data = client.body(message_id)
except nntp.NNTPYEncError as e:
    data = e.data
```

### Posting

```python
message_id = client.post(
    {"From": "me@example.com", "Newsgroups": "alt.test", "Subject": "Hello"},
    "Hello, world!\n",
)
```

`post()` returns the new article's message-id if the server reports it,
otherwise `True`.

### Errors and reconnecting

All errors derive from `nntp.NNTPError`:

- `NNTPReplyError`: an unexpected reply, with `code` and `message`.
  `NNTPTemporaryError` (4xx) and `NNTPPermanentError` (5xx) are subclasses.
- `NNTPDataError`: a response that can't be parsed, such as a bad overview
  line. `NNTPYEncError` is a subclass.
- `NNTPSyncError`: the connection can't be used, see below.

A streamed response must be read to the end before the next command. If one
is abandoned part way (a `break`, or `next()` to take only the first match)
or fails part way, the connection is left out of sync and the next command
raises `NNTPSyncError`. Use `list()` to read a response before searching it.
Call `reconnect()` to recover; it does not restore the selected group.

```python
for number, overview in client.xover((first, last)):
    if number > 1000:
        break  # The rest of the response is never read

client.reconnect()
client.group("alt.binaries.test")
```

## Supported commands

CAPABILITIES, MODE READER, QUIT, DATE, HELP, NEWGROUPS, NEWNEWS, LIST
(ACTIVE, ACTIVE.TIMES, NEWSGROUPS, OVERVIEW.FMT, HEADERS, EXTENSIONS), GROUP,
LISTGROUP, NEXT, LAST, STAT, ARTICLE, HEAD, BODY, POST, IHAVE, HDR, XHDR,
XZHDR, OVER, XOVER, XZVER, XPAT, COMPRESS DEFLATE and XFEATURE COMPRESS
GZIP.

Other commands can be sent with `client.command()`; read a multi-line
response with `client.info()`.

## License

GPL-3.0-or-later.
