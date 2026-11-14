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
mkdir -p .bin
GOBIN="$PWD/.bin" go install github.com/go-while/nntp-testkit/cmd/nntp-fakeserver@v0.13.0
PYNNTP_FAULT_SERVER=.bin/nntp-fakeserver uv run pytest tests/faults -rxX
```

A full run takes about two minutes on a 1-core test VM; `-k gzip` and the like
select a subset.
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
free-threaded builds) a memory limit. After the action the client sends DATE:
a scenario passes when the client returns the right result, or raises an
`NNTPError` or `OSError` and the connection is either still in sync (DATE gets
the right reply) or marked broken (the next command raises `NNTPSyncError`). A
hang, a wrong result, another exception or a connection out of sync fails, as
does a failed server-side check (for example, the client sent AUTHINFO before
TLS).

A scenario with `probe=None` makes no DATE probe after its action. If it expects
a marked connection (`Err(..., state="broken")`), the client is probed anyway,
as a check: a connection the client marked raises `NNTPSyncError` without
sending anything, and one it did not mark gets a DATE reply or a closed socket,
which fails the scenario. A scenario that expects `state="sync"` needs a probe;
this is checked when the scenario is built.

The server must print its summary and exit with status 0. A missing summary or
a non-zero exit fails the scenario, since its server-side checks did not run.

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
expected failure, with the reason shown by `-rxX`. Its `known_why` is a fragment
that the failure reason must contain: a known scenario whose reason lacks it
fails the run, and so does one that reports a server-side problem (a hang, a
crash and a missing summary are not the known bug). A different wrong result
of the same kind still counts as the known failure. When a fix
makes it pass, pytest reports XPASS and the run fails: remove `known=` and
`known_why` in the same change.

| Scenarios | Open bug | Issue |
|---|---|---|
| `leftover/second-status-same-packet`, `leftover/status-after-terminator-same-packet`, `leftover/extra-line-after-greeting`, `leftover/crlf-after-terminator` | Data the server sent after a reply is taken as the reply to the next command | #161 |
| `session/resume-abandoned-iterator-after-reconnect` | A response iterator resumed after reconnect() reads the new connection's reply | #162 |
| `xzver/sync-flushed-not-finished` | XZVER rejects a complete DEFLATE stream that is sync-flushed, not finished | #154 |
| `xzver/last-row-without-crlf` | XZVER drops a last row that has no CRLF | #160 |
| `yenc/stray-yend-after-block` | A =yend line after a complete block raises NNTPYEncError | #159 |
| `malformed/group-negative-numbers` | group() accepts negative numbers in the 211 reply | #157 |

## Updating nntp-testkit

The version is pinned in the command above and in the Faults workflow
(`NNTP_TESTKIT`); change both together.
