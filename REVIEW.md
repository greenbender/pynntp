# Code review

pynntp has about 2,500 lines of Python in `nntp/` (1,800 in `nntp.py`) plus the tests. This
file tracks how much of it has been reviewed, and how. The findings themselves are in
[ISSUES.md](ISSUES.md).

## How it works

- Pick an area from the table, set it to "in progress" and say so where both sides' agents
  read it (`taking: area N`), so two people don't review the same code.
- Each finding gets an ID in ISSUES.md. Each fix gets its own branch from `main` and one pull
  request with a unit test that fails on `main` (the fake socket in `tests/helpers.py`, at the
  recv sizes of the `chunk` fixture). Don't collect unrelated fixes on one branch.
- Before a PR: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict .`
  and `uv run pytest` (with the INN integration tests, see CONTRIBUTING.md); coverage stays at
  100%. Add a CHANGELOG entry under "Unreleased".
- A review of a PR says what was checked: the commit, the tests or probes, the Python and INN
  versions. Security findings go through [SECURITY.md](SECURITY.md), never into this file or a
  public PR.
- When an area is done, set it to "reviewed" with the date and what was used. A later pass
  makes it "reviewed 2×".

Status values: not started, partial (what was looked at), in progress, reviewed (date),
reviewed 2× (date).

## Areas

| # | Area | Code | Lines | What to look at | Status |
|---|---|---|---|---|---|
| 0 | Docs | `README.md`, `index.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `SECURITY.md`, `docs/`, docstrings | | Examples against the API, docstrings against the code, Sphinx build | partial: README command list and CHANGELOG claims (2026-11-09) |
| 1 | Connection and TLS | `BaseNNTPClient.__init__`, `_connect`, `reconnect`, `_enable_tls`, `close`; `NNTPClient.__init__`, `from_url`, `_connect`, `__enter__`, `__exit__`, `quit` | ~250 | Greeting, implicit TLS and STARTTLS (no data before TLS, no login before TLS), MODE READER, reconnect state, URL parsing | reviewed 2× (2026-11-09): P-03, P-12, P-17, P-34 |
| 2 | Commands, status and login | `status`, `command`, `_unexpected`, `_command`, `login`, `_sync`, `_MULTILINE` | ~170 | Status line grammar, expected codes, unexpected multi-line replies, 480 login, credentials, CR/LF/NUL, encoding, sync state | reviewed 2× (2026-11-09): P-01, P-02, P-04, P-11, P-21, P-29 |
| 3 | Reading and compression | `_recv`, `_decompress`, `_send`, `_readline`, `_read`, `_info_plain`, `_info_gzip`, `_info_yenczlib`, `_info`, `_response`, `info`, `compress`, `xfeature_compress_gzip`; `fifo.py` | ~320 | Dot-unstuffing, terminators split across reads, gzip with and without TERMINATOR, XZVER/XZHDR, COMPRESS DEFLATE, abandoned iterators | reviewed 2× (2026-11-09): P-06, P-07, P-22, P-23, P-28, P-30, P-31 |
| 4 | Articles and yEnc | `_read_body`, `_decode_yenc`, `article`, `head`, `body`; `yenc.py` | ~310 | yEnc detection, blocks and text around them, size/CRC/part trailer checks, ARTICLE status parsing | reviewed 2× (2026-11-09): P-05, P-08, P-10, P-18, P-27 |
| 5 | Session and group commands | `capabilities`, `mode_reader`, `date`, `help`, `newgroups`, `newnews`, `list*`, `group`, `listgroup`, `next`, `last`, `stat`, `_select`, `_timestamp` | ~360 | Reply codes (RFC 3977), parsing of each reply, GMT timestamps, watermarks, message-id grammar | reviewed (2026-11-09): P-13, P-15, P-19, P-20, P-25, P-32, P-33 |
| 6 | Overview and headers | `_overview_fields`, `overview_fmt`, `_xover`, `xover`, `xzver`, `over`, `_hdr`, `_number_values`, `hdr`, `xhdr`, `xzhdr`, `xpat` | ~200 | OVERVIEW.FMT and `:full` fields, the fallback format, HDR vs XHDR codes and rows | reviewed (2026-11-09): P-14, P-26 |
| 7 | POST and IHAVE | `post`, `ihave`, `_transfer`, `_prepare_article`; `utils.unparse_headers` | ~170 | Preflight before sending, dot-stuffing, spooling large articles, final reply, Message-ID handling, header injection | reviewed (2026-11-09): P-09, P-24 |
| 8 | Helpers | `utils.py`, `headerdict.py`, `types.py` | ~400 | Parsers, ranges, header folding, `HeaderDict` semantics | partial: parsers used by areas 5 to 7 (2026-11-09) |
| 9 | Tests and CI | `tests/`, `.github/workflows/` | | Coverage of the areas above, integration tests on other INN setups, flaky tests | partial: integration tests on stock INN 2.7.2, 2.7.4 and 2.8 (P-35); fault-injection scenarios (2026-11-09) |

Line counts are approximate, from `main` at `48fc0b3`.

## How the 2026-11-09 review was done

- The go-while fork's own cross-reviewed fixes (develop-01) were compared with 3.0.0 to 3.2.0,
  one probe per difference against a fake server; the results are #102 and P-01 to P-21.
- #133 to #138 were reviewed after merging, each run on Linux test VMs (Python 3.11 to 3.15).
- Upstream's suite with its integration tests ran against INN 2.7.2 and 2.7.4 (Ubuntu inn2) and
  INN 2.8 (InterNetNews/inn main, built from source); see P-35.
- 225 fault-injection scenarios ran over real TCP, TLS and COMPRESS against the nntp-testkit
  fake server (split and slow writes, drops, unexpected and malformed replies): P-27 to P-31.

## Findings

Fixed so far (the open ones are in [ISSUES.md](ISSUES.md)).

| Finding | Area | Found by | Fixed in |
|---|---|---|---|
| P-01 unexpected multi-line reply left unread | 2 | comparison with the fork | #133 |
| P-02 480 to AUTHINFO recursed | 2 | comparison with the fork | #133 |
| P-03 QUIT failing in `__exit__` replaced the block's exception | 1 | comparison with the fork | #133 |
| P-04 commands encoded strictly | 2 | comparison with the fork | #133 |
| P-05 text after `=yend` decoded | 4 | comparison with the fork | #134 |
| P-06 gzip trailer left unread with TERMINATOR | 3 | comparison with the fork | #134 |
| P-07 truncated XZVER stream dropped a row | 3 | comparison with the fork | #134 |
| P-08 lenient yEnc trailer checks | 4 | comparison with the fork | #134 |
| P-09 `post()` missed INN's Message-ID | 7 | comparison with the fork | #135 |
| P-10 yEnc detected on any `=y` line | 4 | comparison with the fork | #134, #136 |
| P-11 credentials checked after AUTHINFO USER | 2 | comparison with the fork | #133 |
| P-12 MODE READER after authentication | 1 | comparison with the fork | #133 |
| P-13 STAT and LISTGROUP accepted invalid values | 5 | comparison with the fork | #135, #136 |
| P-14 legacy XHDR rows rejected | 6 | comparison with the fork | #135 |
| P-15 years before 1000 not padded | 5 | comparison with the fork | #135 |
| P-16 `.coverage` committed | 9 | review | #133 |
| P-17 raw STARTTLS commands passed the guard | 1 | review of #128 | #133 |
| P-18 forced yEnc decoding without `=ybegin` | 4 | comparison with the fork | #134 |
| P-19 iterator methods sent their command on first `next()` | 5 | comparison with the fork | #138 |
| P-20 message-id pattern requiring `@` | 5 | review of #135 | #141 |
| P-21 `login()` left a multi-line reply unread | 2 | review of #133 | #141 |
| P-30 abandoned iterator broke the reconnected connection | 3 | fault injection | #138 |
| P-32 huge LIST ACTIVE.TIMES timestamps | 5 | review of #138 | #138 |

## Checked, not a defect

- STARTTLS: data after the 382 in the same read raises `NNTPProtocolError`; in a later read the
  TLS handshake fails; 580/200 to STARTTLS fail the constructor with no plain-text fallback; no
  AUTHINFO is sent before TLS in any spelling of the command (P-17 checked for bypasses).
- No AUTHINFO after COMPRESS, also not on a 480 (RFC 8054).
- `BytesFifo`: `len()` counts queued writes, compaction is amortized; the block yEnc decoder
  completes an escape split across reads.
- INN-style `Z_PARTIAL_FLUSH` COMPRESS replies decode without waiting for more data.
- LISTGROUP's unparsed 211 line (P-33).
