# Changelog

## Unreleased

### Breaking changes

- `list_active()` and `newgroups()` returned the low and high water marks
  swapped; RFC 3977 orders them high, low. `Newsgroup.low` and
  `Newsgroup.high` are now correct (#111).
- `body()` and `article()` check yEnc data against its size and CRC and raise
  `NNTPYEncError` on a mismatch, instead of returning corrupt data. The
  decoded data is in the error's `data` attribute (#117).
- `xover()` returns "full" fields (such as Xref) without the header name,
  and keeps trailing empty fields. `hdr()`, `xhdr()` and `xzhdr()` values no
  longer end in CRLF (#112).
- `post()` raises `ValueError`, before sending anything, for header names or
  values that could inject headers or end the article (CR, LF or NUL other
  than a folded line) (#111).
- Unparseable responses to `date()`, `newgroups()`, `list_active()`, `head()`
  and `article()` raise `NNTPDataError` rather than `ValueError` (#118).
- `HeaderDict` iterates over plain `str` names (previously case-insensitive
  name objects), no longer raises `TypeError` for non-string names or values,
  and no longer compares equal to a list of pairs (#110).
- `post()` checks the whole body before sending anything: a line with CR or
  NUL raises `NNTPDataError` and nothing is posted. Previously the article was
  posted, cut short before that line (#42).
- Commands containing CR, LF or NUL (for example in a group name or
  message-id) raise `ValueError` instead of being sent (#42).
- `nntp.fifo` contains only `BytesFifo` with `write`, `read`, `readline`,
  `len()` and iteration (#108).

### Added

- `ihave()` for the IHAVE command (#42).
- `compress()` for RFC 8054 COMPRESS DEFLATE, compressing the whole
  connection (#41).
- `over()`, `stat()` and `listgroup()` for the RFC 3977 OVER, STAT and
  LISTGROUP commands (#43, #45, #46).
- `reconnect()` reopens the connection with the same settings (#116).
- `NNTPYEncError`, and `NNTPSyncError` exported from `nntp` (#112, #117).
- Folded header values in `post()` (#111).
- Tested on Python 3.14, 3.14 free-threaded and 3.15 (#115).

### Fixed

- Article lines starting with "." were returned twice, which corrupted yEnc
  bodies (#107, issue #99).
- gzip-compressed responses could be corrupted or hang when the terminator
  was split across reads (#107).
- `hdr()` always failed: it expected reply 221, but HDR replies 225 (#43).
- `newgroups()` and `newnews()` always failed: the timestamp used "UTC" where
  servers require "GMT" (#112).
- An abandoned or failed response left the connection unusable;
  `NNTPSyncError` now says why, and `reconnect()` recovers (#116).
- Leaving a `with` block after `close()` or `quit()` raised `OSError`
  (#112).
- The socket leaked when connecting failed (#112).
- `parse_date()` accepted dates with extra digits, and `parse_headers()`
  failed on LF-only blank lines (#111).
- yEnc trailers with a CRC of fewer than 8 hex digits were rejected (#107).
- Several `IndexError` crashes on empty status lines and replies (#112).

### Performance

- yEnc decoding about 4x faster (#107).
- `BytesFifo` line reading linear rather than quadratic on large buffers
  (#108).
- `HeaderDict` 2-6x faster; XOVER about 1.4x faster end to end (#110, #112).

## 2.0.1 - 2024-09-29

- `timeout`, `use_ssl`, `ssl_mode` and `reader` are keyword-only in
  `NNTPClient` and `BaseNNTPClient` (#49).

## 2.0.0 - 2024-09-29

- Requires Python 3.9 or newer.
- `timeout`, `use_ssl` and `reader` must be passed as keyword arguments.
- Type hints.
- STARTTLS and implicit TLS via the `ssl_mode` argument.
- Fixed newsgroup descriptions being truncated at the first space.

Older releases: <https://github.com/greenbender/pynntp/releases>
