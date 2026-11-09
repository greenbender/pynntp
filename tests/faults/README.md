# Fault-injection tests

These tests run pynntp over real TCP against the fake server of
[nntp-testkit](https://github.com/go-while/nntp-testkit), with faults injected:
replies split into single bytes, slow or stalled servers, dropped and reset
connections, unexpected or malformed replies, and broken TLS, COMPRESS DEFLATE,
XFEATURE COMPRESS GZIP, XZVER and yEnc data. They complement the unit tests,
whose fake socket cannot drop a TLS session or stall a handshake.

They are opt-in: the default `uv run pytest` does not collect them, and they
need Go (1.26 or later).

## Run

```sh
(cd tests/faults/server && go build -o faultserver .)
PYNNTP_FAULT_SERVER=tests/faults/server/faultserver uv run pytest tests/faults -rxX
```

A full run takes a minute or two; `-k gzip` and the like select a subset.
The [Faults](../../.github/workflows/faults.yml) workflow does the same; start
it from the Actions tab (it also runs weekly).

## How it works

`server/` is a small Go program that serves one scenario: either a byte-exact
script (read a client line, write these bytes in these pieces, then close,
reset, start TLS or start reading DEFLATE), or the fake server's Store or IHAVE
peer with its faults. It prints its address, serves until stdin closes, then
prints what it saw.

`scenarios.py` holds the scenarios. For each one, `run()` starts the server and
runs the client in a child process under a deadline and a memory limit. After
the action the client sends DATE: a scenario passes when the client returns the
right result, or raises an `NNTPError` or `OSError` and the connection is
either still in sync (DATE gets the right reply) or marked broken (the next
command raises `NNTPSyncError`). A hang, a wrong result, another exception or
a connection out of sync fails, as does a failed server-side check (for
example, the client sent AUTHINFO before TLS).

```python
yield Scenario(
    "split/body-dotstuff-bytewise",
    script(step("BODY 1", bytewise(ml(b"222 1 <a@b>", BODY_LINES)))),
    lambda c: c.body(1, decode=False),
    Ok(BODY_BYTES),
    RAW,  # NNTPClient keyword arguments
)
```

## Known failures

A scenario with `known=` set fails on the current code and is marked as an
expected failure, with the reason shown by `-rxX`. When a fix makes it pass,
pytest reports XPASS and the run fails: remove `known=` in the same change.

| Scenarios | Open bug |
|---|---|
| `leftover/second-status-same-packet`, `leftover/status-after-terminator-same-packet`, `leftover/extra-line-after-greeting` | Data the server sent after a reply is taken as the reply to the next command |
| `code/authinfo-user-215-block` | login() raises on an unexpected multi-line reply without reading it |
| `session/abandoned-iterator-finalized-after-reconnect`, `session/iterator-variable-reused-after-reconnect`, `session/header-parse-error-then-reconnect` | A response iterator closed after reconnect() marks the new connection broken (the last one only on some Python versions) |
| `session/resume-abandoned-iterator-after-reconnect` | A response iterator resumed after reconnect() reads the new connection's reply |
| `gzip/terminator-inside-trailer-late` | With TERMINATOR, the reply waits for the end of the gzip stream after the terminator |
| `xzver/sync-flushed-not-finished` | XZVER rejects a complete DEFLATE stream that is sync-flushed, not finished |
| `xzver/last-row-without-crlf` | XZVER drops a last row that has no CRLF |
| `yenc/stray-yend-after-block` | A =yend line after a complete block raises NNTPYEncError |
| `malformed/listgroup-invalid-211` | LISTGROUP does not check the numbers in its 211 reply |
| `malformed/group-negative-numbers` | group() accepts negative numbers in the 211 reply |
| `malformed/stat-msgid-without-at` | stat() rejects a message-id without @, which RFC 3977 allows |

## Updating nntp-testkit

`server/go.mod` pins a tagged nntp-testkit release. To update it:

```sh
cd tests/faults/server && go get github.com/go-while/nntp-testkit@vX.Y.Z && go mod tidy
```
