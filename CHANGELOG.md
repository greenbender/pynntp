# Changelog

## 3.3.0 - 2026-11-12

### Added

- `utils.parse_active_time()`, `utils.parse_newsgroup_description()`,
  `utils.parse_overview_fmt()` and `utils.parse_number_value()` parse LIST
  ACTIVE.TIMES, LIST NEWSGROUPS, LIST OVERVIEW.FMT and HDR/XHDR/XPAT lines,
  like `utils.parse_newsgroup()` does for LIST ACTIVE (#138).

### Changed

- Methods that return an iterator (`capabilities()`, `newgroups()`,
  `newnews()`, the `list_*()` methods, `listgroup()`, `hdr()`/`xhdr()`/
  `xzhdr()`, `xover()`/`xzver()`/`over()` and `xpat()`) send their command and
  read its status line when called, as `list()` already did, instead of on the
  first `next()`. A reply error is raised by the call, and another command
  issued before the iterator is read raises `NNTPSyncError` (#102, #138).

### Security

Found by go-while.

- Response lines longer than `max_line_length` (64 KiB including CRLF by
  default; set it on the class or a client) raise `NNTPDataError` and mark the
  connection out of sync, instead of being buffered without limit. This
  includes lines decompressed from COMPRESS DEFLATE, XFEATURE COMPRESS GZIP and
  XZVER/XZHDR, which are inflated at most 64 KiB at a time. Reading a long
  partial line is no longer quadratic (#148).
- A server answering 480 again after a successful login made the client log
  in and retry until `RecursionError`. The command is now retried once, and a
  second 480 is raised (#146).
- `login()` checks that both AUTHINFO commands fit the 512-octet limit before
  sending anything. A password that was too long was refused only after
  `AUTHINFO USER` was sent, and the error contained it. Errors for AUTHINFO
  commands no longer show their arguments (#147).
- The default TLS context requires TLS 1.2 or newer. Python leaves the
  minimum to OpenSSL's configuration, which may allow TLS 1.0 and 1.1. A
  given `ssl_context` is used as it is (#142).

### Fixed

Found by go-while (#102).

- 3.2.0 added a second message-id pattern requiring `@`, which replaced the
  RFC 3977 one: STAT, NEXT and LAST rejected valid message-ids without `@`
  (served by INN with `laxmid`), and `ihave()` accepted non-ASCII message-ids
  and ones over 250 octets. The RFC 3977 pattern is used everywhere again
  (#141).
- `login()` left an unexpected multi-line reply to AUTHINFO unread, so the
  next command read its data as a reply (#141).
- An iterator abandoned before `reconnect()` no longer marks the new
  connection broken when it is closed or garbage-collected (#138).
- An empty line in a LIST NEWSGROUPS response raised `IndexError` instead of
  `NNTPDataError` (#138).
- A LIST ACTIVE.TIMES timestamp too large for `datetime` raised
  `OverflowError` or `OSError` instead of `NNTPDataError`;
  `utils.parse_epoch()` raises `ValueError` for it (#138).
- `date()` raised `NNTPDataError` when the server added text after the date,
  which RFC 3977 allows (section 3.2), e.g. `111 20240102030405 server time`
  (#149).

## 3.2.0 - 2026-11-09

### Changed

- `body(decode=True)` and `article(decode=True)` raise `NNTPYEncError` when
  the body has no `=ybegin` line, instead of returning plain text run through
  the yEnc decoder (#102).
- A 480 reply to `mode_reader()` or a raw MODE READER is raised instead of
  logging in and repeating MODE READER, which RFC 4643 2.2 forbids. Call
  `login()` instead (#102).

### Fixed

Found by go-while (#102).

- An unexpected reply with a multi-line block (e.g. 222 to HEAD, or to the
  end of a POST) left the block unread, so the next command read a data line
  as its reply. The block is now read before `NNTPReplyError` is raised.
- `command()` only skipped the login before STARTTLS when the verb was exactly
  `STARTTLS`; `command(" STARTTLS ")` or `command("", "STARTTLS")` still sent
  the credentials in clear text. Commands are now matched by their first word.
- A server answering AUTHINFO with 480 made the client log in again
  recursively, until `RecursionError`.
- After a 480 to MODE READER while connecting, the client logs in and no
  longer repeats MODE READER (RFC 4643 2.2).
- `login()` rejects a username or password containing CR, LF or NUL before
  sending anything, and raises rather than sending an empty `AUTHINFO PASS`.
- Command arguments are encoded with the client's `errors` policy, so a group
  name read from LIST ACTIVE with non-UTF-8 bytes can be selected.
- A failing QUIT at the end of a `with` block no longer replaces the
  exception raised inside the block.
- `.coverage` was committed by mistake; it is now ignored.
- Text outside a yEnc block, such as a signature after `=yend`, was decoded
  into the data. It is now dropped.
- yEnc detection needs `=ybegin ` at the start of the body; a plain body
  starting with `=ybeginners guide` was treated as yEnc and raised.
- yEnc trailers must have a valid `size=`; an invalid `crc32=`, `pcrc32=` or
  `part=` (e.g. 9 hex digits) is reported rather than ignored.
- XFEATURE COMPRESS GZIP TERMINATOR: when the end of the gzip stream arrived
  after the decompressed terminator, it was left unread and the next reply
  failed to parse.
- XZVER/XZHDR: a DEFLATE stream that ends early raises `NNTPDataError` instead
  of silently dropping the last, partial row.
- `post()` returned `True` for INN's `240 Article received <id>`: the
  message-id was only looked for in the first word. It is now found anywhere
  in the reply.
- STAT, NEXT and LAST reject a negative article number or a missing or
  malformed message-id; LISTGROUP rejects article numbers below 1.
- XHDR by message-id accepts the older `<id> value` rows, giving article
  number 0 as HDR does.
- NEWGROUPS and NEWNEWS zero-pad years before 1000 (`%Y` is not padded on
  every platform).

## 3.1.0 - 2026-11-09

### Added

- `login()` logs in straight away instead of waiting for the server to ask;
  use it before `compress()` (RFC 4643).

### Fixed

- A 480 reply to STARTTLS no longer makes the client log in, which sent the
  password in clear text before TLS was set up.
- `article()` detects yEnc from the body only, as `body()` does; a "yEnc"
  Subject no longer forces decoding of a plain body.
- `article()` with a malformed article number in its 220 reply reads the
  article before raising `NNTPDataError`, so the connection stays in sync.
- Only a permanent (5xx) error to LIST OVERVIEW.FMT selects the default
  overview format. Other errors are raised and the format is retried next
  time instead of the default being cached.
- POST and IHAVE encode headers with the client's `errors` policy
  (`surrogateescape`), so headers read from an article can be sent back.
- Status lines must start with exactly three digits, 1xx to 5xx (RFC 3977).
  Codes such as `0411` or `+411` raise `NNTPProtocolError`.

## 3.0.0 - 2026-11-09

### Breaking changes

- The undocumented `PYNNTP_TLS_INSECURE` environment variable, which
  disabled certificate checks, is gone. Pass an `ssl_context` instead (#123).
- `xpat()` yields `(article number, header value)` like `hdr()`, not raw
  lines (#123).
- `xfeature_compress_gzip()` returns `None` rather than `True` (#123).
- Command lines longer than 512 octets (RFC 3977) raise `ValueError` instead
  of being sent (#123).
- Requires Python 3.11 or newer; 3.9 and 3.10 are end-of-life (#114).
- `SSLMode` is a `StrEnum`: `str(SSLMode.IMPLICIT)` is now `"implicit"`
  rather than `"SSLMode.IMPLICIT"`. Comparisons with strings are unchanged
  (#114).
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

- `NNTPClient.from_url()` connects from a URL such as
  `nntps://user:password@news.example.com` (schemes `nntp`, `nntps`/`snews` and
  `nntp+starttls`). The query can set `timeout` and `reader`; keyword
  arguments override the URL.
- `port` defaults to 563 with implicit TLS (119 otherwise), and `timeout`
  accepts a float or `None` (#123).
- `ssl_context` argument for TLS settings, such as a private CA (#123).
- `command(..., expect=...)` raises `NNTPReplyError` for any other reply
  (#123).
- `HeaderDict`, `Newsgroup`, `Range` and `SSLMode` are exported from `nntp`
  (#123).
- Type hints are shipped for type checkers (`py.typed`), and the LICENSE is
  included in the sdist and wheel (#123).
- `ihave()` for the IHAVE command (#42).
- `compress()` for RFC 8054 COMPRESS DEFLATE, compressing the whole
  connection (#41).
- `over()`, `stat()` and `listgroup()` for the RFC 3977 OVER, STAT and
  LISTGROUP commands (#43, #45, #46).
- `reconnect()` reopens the connection with the same settings (#116).
- `NNTPYEncError`, and `NNTPSyncError` exported from `nntp` (#112, #117).
- Folded header values in `post()` (#111).
- Tested on Python 3.11 to 3.15, including 3.14 free-threaded (#114, #115).

### Fixed

- An empty XZVER or XZHDR response raised `RuntimeError`; it now yields
  nothing. XZVER/XZHDR trailers are also checked for size, like `body()`
  (#123).
- `unparse_range(True)` returned `"True"`; booleans are now rejected (#123).
- STARTTLS: data arriving with the 382 reply, before TLS starts, is rejected
  rather than read later as if protected (#123).
- A server asking for a login when no username is set no longer gets a bare
  `AUTHINFO USER` (#123).
- `LIST OVERVIEW.FMT` accepts `:full` in any case (#123).
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

- yEnc bodies are decoded a block at a time: `body()` about 1.8x faster
  (#123).
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
