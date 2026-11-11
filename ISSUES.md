# Known Issues

Findings about pynntp since 3.0.0 (2026-11-09). They come from reviews of the code, the
offline tests, the INN integration tests on several INN versions (2.6.4, 2.7.x, 2.8 from
source), and fault-injection runs against the nntp-testkit fake server. [REVIEW.md](REVIEW.md)
tracks how much of the code has been reviewed.

Function names refer to `main` at `48fc0b3` unless a PR is named. "Verified" means reproduced
with a fake server or a test; "from review" means reported by a review pass and not
reproduced yet.

Status values: **open**, **PR** (fix waiting for review, with its number), **fixed** (with
the PR that fixed it), **decision** (needs the maintainer to decide), **won't fix**.

Security problems are reported privately (see [SECURITY.md](SECURITY.md)); they are listed
here only after their advisory has been published, or once they are rated low and moved to a
public PR (P-36 to P-38).

Last updated 2026-11-11: P-36 to P-38 moved from private reports to public PRs, P-39 found by
running the tests against another server, P-40 to P-42 from a review of `utils.py`,
`headerdict.py` and `types.py`. P-01 to P-35 (2026-11-09) came from #102, the reviews of #133
to #138, the fault-injection run and the INN integration runs.

| ID | Severity | Summary | Status |
|---|---|---|---|
| [P-01](#p-01-unexpected-multi-line-reply-left-unread) | High | An unexpected multi-line reply was left unread; the next command read its data | fixed (#133) |
| [P-02](#p-02-480-to-authinfo-recursed) | Medium | A 480 reply to AUTHINFO itself made `login()` recurse | fixed (#133) |
| [P-03](#p-03-quit-failing-in-__exit__-replaced-the-blocks-exception) | Medium | QUIT failing in `__exit__` replaced the `with` block's exception | fixed (#133) |
| [P-04](#p-04-commands-encoded-strictly) | Medium | Commands encoded strictly: 8-bit group names from LIST ACTIVE couldn't be used | fixed (#133) |
| [P-05](#p-05-text-after-yend-decoded-into-the-data) | Medium | Text after `=yend` decoded into the data | fixed (#134) |
| [P-06](#p-06-gzip-trailer-left-unread-with-terminator) | Medium | gzip trailer left unread with XFEATURE COMPRESS GZIP TERMINATOR | fixed (#134) |
| [P-07](#p-07-truncated-xzver-stream-dropped-a-row) | Medium | A truncated XZVER/XZHDR stream silently dropped its last row | fixed (#134) |
| [P-08](#p-08-lenient-yenc-trailer-checks) | Medium | yEnc trailer: missing `size=` skipped the size check; malformed values accepted | fixed (#134) |
| [P-09](#p-09-post-missed-inns-message-id) | Medium | `post()` returned True for INN's `240 Article received <id>` | fixed (#135) |
| [P-10](#p-10-yenc-detected-on-any-y-line) | Low | A plain body starting with `=ybeginners` was taken for yEnc | fixed (#134, #136) |
| [P-11](#p-11-credentials-checked-after-authinfo-user) | Low | Credentials checked only after `AUTHINFO USER` was sent; empty password sent a bare `AUTHINFO PASS` | fixed (#133) |
| [P-12](#p-12-mode-reader-after-authentication) | Low | MODE READER sent again after logging in (RFC 4643 2.2) | fixed (#133) |
| [P-13](#p-13-stat-and-listgroup-accepted-invalid-values) | Low | STAT and LISTGROUP accepted negative numbers and missing message-ids | fixed (#135, #136) |
| [P-14](#p-14-legacy-xhdr-rows-rejected) | Low | XHDR by message-id: legacy `<id> value` rows raised `NNTPDataError` | fixed (#135) |
| [P-15](#p-15-years-before-1000-not-padded) | Low | NEWGROUPS/NEWNEWS: years before 1000 weren't zero-padded | fixed (#135) |
| [P-16](#p-16-coverage-committed) | Low | `.coverage` committed | fixed (#133) |
| [P-17](#p-17-raw-starttls-commands-passed-the-guard) | Low | `command(" STARTTLS ")` and `command("", "STARTTLS")` still logged in on a 480 | fixed (#133); TLS-level compression part won't fix |
| [P-18](#p-18-forced-yenc-decoding-without-ybegin) | — | `decode=True` on a body without `=ybegin` returned garbage | fixed (#134, decided: raise) |
| [P-19](#p-19-iterator-methods-sent-their-command-on-first-next) | — | Iterator methods sent their command on the first `next()` | fixed (#138, decided: at call time) |
| [P-20](#p-20-message-id-pattern-requiring-) | Medium | 3.2.0: a second message-id pattern required `@` (STAT/NEXT/LAST rejected valid ids) | fixed (#141) |
| [P-21](#p-21-login-left-a-multi-line-reply-unread) | Low | `login()` left an unexpected multi-line reply unread | fixed (#141) |
| [P-22](#p-22-xzver-requires-a-finished-deflate-stream) | Medium | XZVER/XZHDR raise when the DEFLATE stream is complete but not finished | decision |
| [P-23](#p-23-gzip-terminator-waits-for-the-stream-end) | Low | gzip TERMINATOR: the client waits for the end of the gzip stream after the terminator | decision |
| [P-24](#p-24-post-takes-the-first-message-id-in-the-reply) | Low | `post()` takes the first `<…@…>` anywhere in the 240 reply | open |
| [P-25](#p-25-group-returns-unchecked-numbers) | Low | `group()` returns negative or odd counts unchecked | open |
| [P-26](#p-26-any-token-starting-with--gets-number-0) | Low | HDR/XHDR/XPAT: any first token starting with `<` gets number 0 | open |
| [P-27](#p-27-stray-yend-checked-as-a-trailer) | Low | A stray `=yend` outside a block is checked as a trailer | open |
| [P-28](#p-28-last-xz-row-without-crlf-dropped) | Low | A finished XZ stream's last row without CRLF is dropped silently | open |
| [P-29](#p-29-unread-data-before-a-command) | Medium | Data the server sent beyond a reply becomes the next command's reply | open |
| [P-30](#p-30-abandoned-iterator-broke-the-reconnected-connection) | Low | An abandoned iterator closed after `reconnect()` marked the new connection broken | fixed (#138) |
| [P-31](#p-31-resumed-iterator-reads-the-new-connection) | Low | An old iterator resumed after `reconnect()` reads the new connection's lines | open |
| [P-32](#p-32-huge-list-activetimes-timestamps) | Low | Huge LIST ACTIVE.TIMES timestamps raised `OSError`/`OverflowError` | fixed (#138) |
| [P-33](#p-33-listgroup-211-metadata-not-checked) | — | LISTGROUP doesn't check its 211 line | won't fix (not used) |
| [P-34](#p-34-tls-minimum-left-to-openssl) | Low | The default TLS context leaves the minimum version to OpenSSL's configuration | PR (#142) |
| [P-35](#p-35-integration-tests-assume-the-docker-image) | Low | The integration tests assume a fresh `greenbender/inn` container | PR (#144) |
| [P-36](#p-36-second-480-after-login-retried-until-recursionerror) | Low | A server answering 480 again after a successful login made the client log in again until `RecursionError` | PR (#146) |
| [P-37](#p-37-too-long-password-sent-after-authinfo-user-and-shown-in-the-error) | Low | A password too long for the command limit was refused only after `AUTHINFO USER`, and the error showed it | PR (#147) |
| [P-38](#p-38-no-limit-on-response-line-length) | Low | No limit on the length of a response line, also after decompression; long lines cost quadratic time | PR (#148) |
| [P-39](#p-39-date-with-text-after-the-date) | Low | `date()` raised for `111 <date> <text>`, which RFC 3977 allows | PR (#149) |
| [P-40](#p-40-header-unfolding-drops-whitespace) | Low | Header unfolding drops the whitespace before a fold | open |
| [P-41](#p-41-empty-overview-field-names-accepted) | Low | LIST OVERVIEW.FMT: empty or space-padded field names accepted | open |
| [P-42](#p-42-range-tuples-accept-bool-bounds) | Low | A range tuple with `True`/`False` is sent as `True-` instead of raising | open |

## Details

### P-01 Unexpected multi-line reply left unread
`command(expect=...)` raised `NNTPReplyError` for an unexpected multi-line 2xx (e.g. HEAD
answered with 222) without reading its data block, and didn't mark the connection broken. The
next command read a data line as its status: `date()` returned 1999-01-01 from a leftover body
line `111 19990101000000`. Also the final reply of POST/IHAVE. Verified. Fixed by reading the
block before raising (`_MULTILINE`); an unexpected 211 marks the connection out of sync.

### P-02 480 to AUTHINFO recursed
A server answering AUTHINFO itself with 480 made `login()` call itself until `RecursionError`.
Verified.

### P-03 QUIT failing in __exit__ replaced the block's exception
`with client:` where QUIT failed (`NNTPError` or `OSError`) raised that instead of the
exception from inside the block. Verified.

### P-04 Commands encoded strictly
Replies are decoded with `surrogateescape`, commands were encoded strictly, so a group name with
8-bit bytes from LIST ACTIVE raised `UnicodeEncodeError` in `group()`. Verified.

### P-05 Text after =yend decoded into the data
`_decode_yenc()` decoded every non-`=y` line, also a signature after `=yend`, and appended the
result to the data. Verified.

### P-06 gzip trailer left unread with TERMINATOR
With XFEATURE COMPRESS GZIP TERMINATOR, when the gzip trailer arrived after the decompressed
terminator, it stayed on the connection and the next status line failed to parse. Verified.

### P-07 Truncated XZVER stream dropped a row
A DEFLATE stream cut short inside a valid yEnc wrapper returned the complete rows and silently
dropped the partial last one. Verified. See also P-22.

### P-08 Lenient yEnc trailer checks
A `=yend` without `size=` skipped the size check; `size=123garbage`, a 9-digit CRC or
`part=x` were accepted. Verified.

### P-09 post() missed INN's Message-ID
Only the first word of the 240 reply was taken as the Message-ID, so INN's
`240 Article received <id>` returned True. Verified. See P-24.

### P-10 yEnc detected on any =y line
Body detection used `startswith(b"=y")`. Verified.

### P-11 Credentials checked after AUTHINFO USER
A password containing LF was refused only after `AUTHINFO USER` had been sent, and an empty
password sent a bare `AUTHINFO PASS`. Verified.

### P-12 MODE READER after authentication
After a 480 login during setup, MODE READER was sent again; RFC 4643 2.2: "the client MUST NOT
issue a MODE READER command after authentication". Verified.

### P-13 STAT and LISTGROUP accepted invalid values
`223 -1 <a@b>`, `223 1 no-id` and LISTGROUP rows `0`/`-1` were accepted. Verified.

### P-14 Legacy XHDR rows rejected
Older servers answer XHDR by message-id with `<id> value` rows instead of `0 value`. Verified.

### P-15 Years before 1000 not padded
`%Y` is platform-dependent; year 1 gave `10102` instead of `00010102`. Verified.

### P-16 .coverage committed
Committed by mistake in #128.

### P-17 Raw STARTTLS commands passed the guard
The guard against logging in before STARTTLS compared the verb exactly. Verified. The second
part of the finding (refusing MODE READER, STARTTLS and COMPRESS through `command()` while
TLS-level compression is active) won't be done: OpenSSL disables TLS compression.

### P-18 Forced yEnc decoding without =ybegin
Inherited from 2.0.1; decided in #134 to raise `NNTPYEncError`.

### P-19 Iterator methods sent their command on first next()
Inconsistent with `list()`; decided in #138 to send at call time.

### P-20 Message-id pattern requiring @
#135 added a second `_MESSAGE_ID_RE` that required `@` and replaced the RFC 3977 one, so STAT,
NEXT and LAST rejected message-ids without `@`, which INN serves with `syntaxchecks:
[laxmid]` (e.g. `<bnews.sri-arpa.865>`), and `ihave()` accepted non-ASCII ids and ids over 250
octets. Verified.

### P-21 login() left a multi-line reply unread
The P-01 fix didn't cover `login()`: an unexpected multi-line reply to AUTHINFO USER/PASS made
the next `date()` return 1999 from the leftover data. Verified.

### P-22 XZVER requires a finished DEFLATE stream
Since #134, `_info_yenczlib()` raises "compressed data is truncated" and breaks the connection
when the DEFLATE data is complete and CRC-correct but only sync-flushed (no final block). If a
provider sends XZVER that way, every XZVER fails. The alternative is to reject only a leftover
partial line (P-07). Verified with a fake server; no real provider checked yet.

### P-23 gzip TERMINATOR waits for the stream end
Since #134, `_info_gzip()` reads after the terminator until the gzip stream ends; a server that
never finishes it makes the client wait for the socket timeout and then breaks the connection,
although all rows and the terminator had arrived. Verified with a fake server; no real
provider checked yet.

### P-24 post() takes the first Message-ID in the reply
A 240 reply such as `posted ok; report abuse to <abuse@example.net>` returns that address.
Verified.

### P-25 group() returns unchecked numbers
`211 -5 -3 -9 g` returns `(-5, -3, -9, 'g')`; `+3` and `1_0` are accepted by `int()`.
Verified.

### P-26 Any token starting with < gets number 0
`parse_number_value()` gives number 0 to any first token starting with `<`, also in range
queries where a message-id row can't be the answer, and without checking for a closing `>`
(`<broken value`, `<>`). Numbers go through `int()`, so `-1`, `+1` and `1_0` are accepted.
Verified.

### P-27 Stray =yend checked as a trailer
A `=yend` line after a block is closed is checked against the last block's size and CRC and
gives a false `NNTPYEncError`. Verified.

### P-28 Last XZ row without CRLF dropped
A finished XZ stream whose last row has no CRLF returns one row less, with no error. Verified.

### P-29 Unread data before a command
`_command()` doesn't check that the buffer is empty before sending. Anything the server sent
beyond a reply, arriving in the same read, becomes the reply to the next command, and every
later reply is shifted by one: `211 1 1 1 alt.a\r\n211 9 9 9 alt.b\r\n` to `GROUP alt.a` makes
`group("alt.c")` return `(9, 9, 9, 'alt.b')`; an extra line after a terminator or the greeting
makes the next `date()` return it. Verified (fault-injection run). Suggested fix: a non-empty
buffer before sending marks the connection broken and raises `NNTPProtocolError`, as already
done after the STARTTLS 382.

### P-30 Abandoned iterator broke the reconnected connection
Verified (fault-injection run); fixed in #138 by checking the iterator's buffer in `_response()`.

### P-31 Resumed iterator reads the new connection
An old iterator resumed after `reconnect()` still reads lines from the new connection, because
the `_info_*` readers go through `self._readline()`. Misuse, but cheap to close by raising
`NNTPSyncError` when the iterator's buffer is no longer the client's. Verified.

### P-32 Huge LIST ACTIVE.TIMES timestamps
`datetime.fromtimestamp()` raised `OSError`/`OverflowError` for huge values. Fixed in #138.

### P-33 LISTGROUP 211 metadata not checked
`listgroup()` returns only the article numbers and never parses its 211 line, so bad metadata
never becomes wrong data. Not a defect.

### P-34 TLS minimum left to OpenSSL
`ssl.create_default_context()` leaves `minimum_version` to the OpenSSL build and configuration:
TLS 1.2 on some systems, "anything" on others. Fix in #142.

### P-35 Integration tests assume the docker image
On a stock INN (Ubuntu inn2 2.7.2 and 2.7.4, INN 2.8 from source) 10 to 11 integration tests
fail because of server state, not the client: port 563 not listening, groups that already hold
articles, the NEWGROUPS cutoff date, `483 Encryption required` instead of 502 for a plain-text
login, INN 2.8's descriptions ending in a period. Fix, with a CI job against Ubuntu's inn2, in
#144.

### P-36 Second 480 after login retried until RecursionError
After a 480, `_command()` logs in and calls itself again; a server that keeps answering 480
after a successful login made it send `AUTHINFO USER`/`PASS` about 990 times and end in
`RecursionError`. Low: needs a misbehaving or hostile server. Verified; reported privately
first, then moved to a public PR as low impact. Fix in #146: retry once, then raise the 480.

### P-37 Too-long password sent after AUTHINFO USER and shown in the error
The rest of P-11: `login()` checked the credentials for CR, LF and NUL but not their length,
so a password over the 512-octet command limit was refused only after `AUTHINFO USER` was
sent, and the `ValueError` contained the whole `AUTHINFO PASS` line. Verified; moved from a
private report to a public PR. Fix in #147: check both lengths first, and show only the verb
in errors for AUTHINFO commands.

### P-38 No limit on response line length
Lines are buffered until a CRLF arrives, with no limit, and the buffer is joined and searched
again on every read. Decompressed data (COMPRESS DEFLATE, XFEATURE COMPRESS GZIP, XZVER/XZHDR)
goes into the same buffer, so a small compressed reply can grow into a very large line. Low:
needs a misbehaving or hostile server. Verified; moved from a private report to a public PR.
Fix in #148: `max_line_length` (64 KiB including CRLF, adjustable), decompression in 64 KiB
steps, and a `BytesFifo` that only joins complete lines.

### P-39 DATE with text after the date
RFC 3977 section 3.2 allows text after the last argument of a status line. `date()` passed the
whole text to `parse_date()`, so `111 20240102030405 server time` raised `NNTPDataError`.
Found by running the tests against OpenUsenetServer; 2.0.1 does the same. Fix in #149.

### P-40 Header unfolding drops whitespace
`parse_headers()` strips each physical line before it knows whether a continuation follows, so
`X-Text: one \r\n two` becomes `one two`; unfolding (RFC 5322 section 2.2.3) only removes the
CRLF and gives `one  two`. Cosmetic; inherited from 2.0.1. Verified.

### P-41 Empty overview field names accepted
`parse_overview_fmt()` returns `("", False)` for `:` and keeps the spaces in ` :full` and
`Subject :full`. A format with an empty name is cached, and `over()` then returns rows keyed
`""`. Malformed server data only; the connection stays in sync. Verified.

### P-42 Range tuples accept bool bounds
`unparse_range(True)` raises, but `unparse_range((True,))` gives `True-` and
`(False, 1)` gives `False-1`, so `over((True,))` sends `OVER True-`. Caller input only; CR, LF
and NUL are still rejected. Verified.
