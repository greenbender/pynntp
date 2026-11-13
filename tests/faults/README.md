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
go install github.com/go-while/nntp-testkit/cmd/nntp-fakeserver@v0.13.0
PYNNTP_FAULT_SERVER=$(go env GOPATH)/bin/nntp-fakeserver uv run pytest tests/faults -rxX
```

A full run takes a minute or two; `-k gzip` and the like select a subset.
The [Faults](../../.github/workflows/faults.yml) workflow does the same; start
it from the Actions tab (it also runs weekly).

## How it works

`nntp-fakeserver` is nntp-testkit's fake server as a program. It serves one
scenario from a JSON file: either a byte-exact script (read a client line, write
these bytes in these pieces, then close, reset, start TLS or start reading
DEFLATE), or the fake server's Store or IHAVE peer with its faults. It prints its
address, serves until stdin closes, then prints what it saw. The scenarios use
short keys; `kit_config()` turns them into the program's JSON schema.

`scenarios.py` holds the scenarios. For each one, `run()` starts the server and
runs the client in a child process under a deadline and (except on
free-threaded builds) a memory limit. After the action the client sends DATE: a
scenario passes when the client returns the right result, or raises an
`NNTPError` or `OSError` and the connection is either still in sync (DATE gets
the right reply) or marked broken (the next command raises `NNTPSyncError`). A
hang, a wrong result, another exception or a connection out of sync fails, as
does a failed server-side check (for example, the client sent AUTHINFO before
TLS).

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

| Scenarios | Open bug | Issue |
|---|---|---|
| `leftover/second-status-same-packet`, `leftover/status-after-terminator-same-packet`, `leftover/extra-line-after-greeting` | Data the server sent after a reply is taken as the reply to the next command | #161 |
| `session/resume-abandoned-iterator-after-reconnect` | A response iterator resumed after reconnect() reads the new connection's reply | #162 |
| `gzip/terminator-inside-trailer-late` | With TERMINATOR, the reply waits for the end of the gzip stream after the terminator | #155 |
| `xzver/sync-flushed-not-finished` | XZVER rejects a complete DEFLATE stream that is sync-flushed, not finished | #154 |
| `xzver/last-row-without-crlf` | XZVER drops a last row that has no CRLF | #160 |
| `yenc/stray-yend-after-block` | A =yend line after a complete block raises NNTPYEncError | #159 |
| `malformed/group-negative-numbers` | group() accepts negative numbers in the 211 reply | #157 |

## Updating nntp-testkit

The version is pinned in the command above and in the Faults workflow
(`NNTP_TESTKIT`); change both together.
