"""Fault-injection scenarios: pynntp against the nntp-testkit fake server.

A scenario is a config for nntp-testkit's nntp-fakeserver (a byte-exact
script, or the fake server's Store or IHAVE peer with its faults), a client
action and the expected outcome.

run() starts the server, runs the client in a child process under a deadline
and a memory limit, then checks what the server saw. The child is this file
run as a script: ``python scenarios.py NAME HOST PORT CA`` prints one JSON
result.

A scenario passes when the client returns the right result, or raises a clean
error (an NNTPError or OSError) and the connection is either still in sync
(the next command, a DATE probe, gets the right reply) or marked broken (the
next command raises NNTPSyncError). It fails on a hang past the deadline, a
wrong result, any other exception, or a connection that is neither in sync
nor marked broken.
"""

from __future__ import annotations

import base64
import contextlib
import gc
import json
import re
import resource
import ssl
import subprocess
import sys
import sysconfig
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import nntp

JSON = dict[str, Any]
Action = Callable[[nntp.NNTPClient], object]
ServerCheck = Callable[[JSON], str | None]
Pred = Callable[[Any], bool]

CRLF = b"\r\n"
GREETING = b"200 fault server ready\r\n"
PROBE_REPLY = b"111 20200102030405\r\n"
PROBE_TUPLE = (2020, 1, 2, 3, 4, 5)
STALE_DATE = b"111 20990101000000\r\n"
MEMORY_LIMIT = 1 << 30

# ---------------------------------------------------------------------------
# Wire builders


def chunk(data: bytes, gap: float = 0, split: int = 0, sgap: float = 0) -> JSON:
    """One write of data, after gap ms, optionally in pieces of split bytes."""
    return {
        "b64": base64.b64encode(data).decode(),
        "gap_ms": gap,
        "split": split,
        "split_gap_ms": sgap,
    }


def bytewise(data: bytes, sgap: float = 0.3) -> JSON:
    """Data written one byte at a time."""
    return chunk(data, split=1, sgap=sgap)


def step(
    expect: str | None,
    *send: bytes | JSON,
    read_dot: bool = False,
    dot_max: int = 0,
    then: str = "",
    sleep: float = 0,
) -> JSON:
    """Read one client line matching expect (None: none), send, then act.

    then is "", "close", "rst", "hang", "starttls" or "inflate" (read the
    client through DEFLATE from now on).
    """
    return {
        "expect": expect,
        "send": [c if isinstance(c, dict) else chunk(c) for c in send],
        "read_dot": read_dot,
        "dot_max": dot_max,
        "then": then,
        "sleep_ms": sleep,
    }


def script(
    *steps: JSON,
    tls: bool = False,
    greeting: bytes | JSON | None = GREETING,
    probe: bool = True,
) -> JSON:
    """A byte-exact server script: greeting, steps, then the DATE probe."""
    raw = [step(None, greeting)] if greeting is not None else []
    raw += steps
    if probe:
        raw.append(step("DATE", PROBE_REPLY))
    return {"mode": "raw", "tls": tls, "raw": raw}


def stuff(lines: list[bytes]) -> bytes:
    return b"".join((b"." + ln if ln.startswith(b".") else ln) + CRLF for ln in lines)


def ml(status: bytes, lines: list[bytes]) -> bytes:
    """A multi-line reply: status line, dot-stuffed lines, terminator."""
    return status + CRLF + stuff(lines) + b".\r\n"


def joined(lines: list[bytes]) -> bytes:
    return b"".join(ln + CRLF for ln in lines)


class Deflate:
    """The server side of a COMPRESS DEFLATE stream (RFC 8054)."""

    def __init__(self) -> None:
        self.c = zlib.compressobj(wbits=-15)

    def sync(self, data: bytes) -> bytes:
        return self.c.compress(data) + self.c.flush(zlib.Z_SYNC_FLUSH)

    def partial(self, data: bytes) -> bytes:
        return self.c.compress(data) + self.c.flush(zlib.Z_PARTIAL_FLUSH)

    def finish(self, data: bytes) -> bytes:
        return self.c.compress(data) + self.c.flush(zlib.Z_FINISH)


def yenc_lines(data: bytes, line: int = 128) -> list[bytes]:
    out, cur = [], bytearray()
    for b in data:
        e = (b + 42) & 0xFF
        if e in (0, 10, 13, 61):
            cur += bytes([61, (e + 64) & 0xFF])
        else:
            cur.append(e)
        if len(cur) >= line:
            out.append(bytes(cur))
            cur = bytearray()
    if cur:
        out.append(bytes(cur))
    return out


def yenc_block(
    data: bytes,
    name: str = "f.bin",
    part: tuple[int, int] | None = None,
    crc: int | None = None,
    size: int | None = None,
    total_size: int | None = None,
    line: int = 128,
) -> list[bytes]:
    crc = zlib.crc32(data) if crc is None else crc
    size = len(data) if size is None else size
    if part:
        n, total = part
        head = [
            f"=ybegin part={n} total={total} line={line} "
            f"size={total_size or size} name={name}".encode(),
            f"=ypart begin=1 end={size}".encode(),
        ]
        tail = f"=yend size={size} part={n} pcrc32={crc:08x}".encode()
    else:
        head = [f"=ybegin line={line} size={size} name={name}".encode()]
        tail = f"=yend size={size} crc32={crc:08x}".encode()
    return [*head, *yenc_lines(data, line), tail]


def yenc_of(comp: bytes, crc: int | None = None, line: int = 128) -> list[bytes]:
    """XZVER/XZHDR body lines: a yEnc block named xz."""
    crc = zlib.crc32(comp) if crc is None else crc
    return [
        f"=ybegin line={line} size={len(comp)} name=xz".encode(),
        *yenc_lines(comp, line),
        f"=yend size={len(comp)} crc32={crc:08x}".encode(),
    ]


def xz_payload(
    rows: list[bytes], finish: bool = True, crc: int | None = None, line: int = 128
) -> list[bytes]:
    """XZVER/XZHDR body lines: yEnc of the raw DEFLATE of the rows."""
    c = zlib.compressobj(wbits=-15)
    comp = c.compress(b"".join(rows))
    comp += c.flush(zlib.Z_FINISH if finish else zlib.Z_SYNC_FLUSH)
    return yenc_of(comp, crc, line)


def gzip_bytes(data: bytes) -> bytes:
    c = zlib.compressobj(wbits=31)
    return c.compress(data) + c.flush(zlib.Z_FINISH)


# ---------------------------------------------------------------------------
# Scenarios and expectations


@dataclass(frozen=True)
class Ok:
    """A result equal to value, or passing pred."""

    value: object = None
    pred: Pred | None = None
    desc: str = ""


class Err:
    """One of the exception types; state is "sync", "broken" or "any"."""

    def __init__(
        self,
        *types: type[BaseException],
        state: str = "any",
        match: str = "",
        pred: Callable[[BaseException], bool] | None = None,
    ) -> None:
        self.types = types
        self.state = state
        self.match = match
        self.pred = pred


@dataclass(frozen=True)
class Clean:
    """Any result passing pred, or any clean error; in sync or marked broken."""

    pred: Pred | None = None
    desc: str = ""


Expect = Ok | Err | Clean
CLEAN_ERRORS = (nntp.NNTPError, OSError)


@dataclass
class Scenario:
    name: str
    server: JSON
    action: Action | None  # None: only connect
    expect: Expect
    connect: JSON = field(default_factory=dict)
    server_check: ServerCheck | None = None
    known: str = ""  # an open bug: the scenario is expected to fail
    known_strict: bool = True  # False: it fails only on some Python versions
    deadline: float = 20.0
    probe: str | None = "raw"  # raw: the exact 2020 DATE; store: any DATE
    max_s: float | None = None


def lines_with(summary: JSON, prefix: str) -> list[str]:
    return [ln for ln in summary["lines"] if ln.upper().startswith(prefix)]


def no_lines(prefix: str, why: str = "") -> ServerCheck:
    def check(s: JSON) -> str | None:
        got = lines_with(s, prefix)
        return f"server got {got[:3]} {why}".strip() if got else None

    return check


def no_lines_after(marker: str, prefix: str) -> ServerCheck:
    def check(s: JSON) -> str | None:
        upper = [ln.upper() for ln in s["lines"]]
        after = (
            upper[len(upper) - upper[::-1].index(marker) :] if marker in upper else []
        )
        bad = [ln for ln in after if ln.startswith(prefix)]
        return f"server got {bad[:3]} after {marker}" if bad else None

    return check


def max_lines(prefix: str, n: int) -> ServerCheck:
    def check(s: JSON) -> str | None:
        got = len(lines_with(s, prefix))
        return f"server got {got} {prefix!r} lines (max {n})" if got > n else None

    return check


def all_of(*checks: ServerCheck) -> ServerCheck:
    def check(s: JSON) -> str | None:
        return next((why for c in checks if (why := c(s))), None)

    return check


def no_mismatch(s: JSON) -> str | None:
    bad = s["mismatches"]
    return f"unexpected client lines: {bad[:4]}" if bad else None


def article_is(expected: list[str]) -> ServerCheck:
    def check(s: JSON) -> str | None:
        arts = s["articles"]
        if not arts:
            return "server read no article"
        if arts[-1] != expected:
            return f"server read article {arts[-1]!r}, expected {expected!r}"
        return no_mismatch(s)

    return check


def attempt(f: Callable[[], object]) -> object:
    """f's result, or its exception as a string."""
    try:
        return f()
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def compressed(f: Action) -> Action:
    """COMPRESS DEFLATE, then f."""

    def act(c: nntp.NNTPClient) -> object:
        c.compress()
        return f(c)

    return act


def in_group(f: Action) -> Action:
    """GROUP alt.test, then f."""

    def act(c: nntp.NNTPClient) -> object:
        c.group("alt.test")
        return f(c)

    return act


STORE_BODY = ["line one", ".dot line", "..two dots", ".", "", "last"]
STORE_BODY_BYTES = b"line one\r\n.dot line\r\n..two dots\r\n.\r\n\r\nlast\r\n"
BODY_LINES = [b"first", b".", b"..", b".leading", b"", b"middle line", b"last."]
BODY_BYTES = joined(BODY_LINES)
OVERFMT = ml(
    b"215 Order of fields in overview database.",
    [
        b"Subject:",
        b"From:",
        b"Date:",
        b"Message-ID:",
        b"References:",
        b":bytes",
        b":lines",
        b"Xref:full",
    ],
)
OVER_ROWS = [
    b"1\tsubj one\ta@b\tdate\t<1@x>\t\t10\t2\tXref: h alt.test:1",
    b"2\t.dotted subj\ta@b\tdate\t<2@x>\t<1@x>\t20\t3\tXref: h alt.test:2",
]
HEADERS = {"Subject": "s", "From": "a@b", "Newsgroups": "alt.test"}
RAW = {"reader": False}
TLS = {"use_ssl": True, "ca": True}
STARTTLS = {"use_ssl": True, "ssl_mode": "STARTTLS", "ca": True, "reader": False}
CREDS = {"username": "u", "password": "p"}


def store_cfg(**kw: Any) -> JSON:  # noqa: ANN401
    """Store mode: alt.test with three articles (bodies with dot lines)."""
    store: JSON = {
        "groups": [["alt.test", "test group"], ["alt.empty", "empty"]],
        "articles": [
            {
                "group": "alt.test",
                "number": n,
                "msgid": f"<{n}@test>",
                "headers": [f"Subject: article {n}", "From: a@b.example"],
                "body": STORE_BODY,
            }
            for n in (1, 2, 3)
        ],
    }
    store.update(kw.pop("store", {}))
    return {"mode": "store", "store": store, **kw}


def over_check(v: Any) -> bool:  # noqa: ANN401
    return bool(
        len(v) == 2
        and v[0][0] == 1
        and v[1][0] == 2
        and v[1][1]["Subject"] == ".dotted subj"
        and v[0][1]["Xref"] == "h alt.test:1"
    )


def store_over_check(v: Any) -> bool:  # noqa: ANN401
    return bool([n for n, _ in v] == [1, 2, 3] and v[0][1]["Subject"] == "article 1")


# ---------------------------------------------------------------------------
# Open bugs: these scenarios fail on the current code


UNREAD_DATA = (
    "data the server sent after a reply is taken as the reply to the next command"
)


def article_parts(c: nntp.NNTPClient) -> object:
    number, headers, body = c.article(1, decode=False)
    return number, headers["Subject"], body


def split_scenarios() -> Iterator[Scenario]:
    """Replies split into small or byte-sized writes."""
    yield Scenario(
        "split/greeting-bytewise",
        script(greeting=bytewise(GREETING)),
        None,
        Ok("connected"),
        RAW,
    )
    yield Scenario(
        "split/body-dotstuff-bytewise",
        script(step("BODY 1", bytewise(ml(b"222 1 <a@b>", BODY_LINES)))),
        lambda c: c.body(1, decode=False),
        Ok(BODY_BYTES),
        RAW,
    )
    reply = ml(b"222 1 <a@b>", [b".", b"..x", b"y"])
    points = range(1, len(reply))
    yield Scenario(
        "split/body-every-split-point",
        script(
            *[step(f"BODY {k}", reply[:k], chunk(reply[k:], gap=15)) for k in points]
        ),
        lambda c: [c.body(k, decode=False) for k in points],
        Ok([b".\r\n..x\r\ny\r\n"] * len(points)),
        RAW,
        deadline=30,
    )
    yield Scenario(
        "split/status-cr-lf",
        script(
            step(
                "BODY 1",
                b"222 1 <a@b>\r",
                chunk(b"\nx\r\n.\r", gap=20),
                chunk(b"\n", gap=20),
            )
        ),
        lambda c: c.body(1, decode=False),
        Ok(b"x\r\n"),
        RAW,
    )
    art = ml(
        b"220 1 <a@b>",
        [
            b"Subject: folded",
            b"\tcontinued",
            b"From: a@b",
            b"",
            b".body",
            b"..",
            b"end",
        ],
    )
    yield Scenario(
        "split/article-bytewise",
        script(step("ARTICLE 1", bytewise(art))),
        article_parts,
        Ok((1, "folded\tcontinued", b".body\r\n..\r\nend\r\n")),
        RAW,
    )
    yield Scenario(
        "split/xover-bytewise",
        script(
            step("LIST OVERVIEW.FMT", bytewise(OVERFMT)),
            step("XOVER 1-2", bytewise(ml(b"224 ov", OVER_ROWS))),
        ),
        lambda c: list(c.xover((1, 2))),
        Ok(pred=over_check, desc="2 rows"),
        RAW,
    )
    yield Scenario(
        "split/group-bytewise",
        script(step("GROUP alt.test", bytewise(b"211 3 1 3 alt.test\r\n"))),
        lambda c: c.group("alt.test"),
        Ok((3, 1, 3, "alt.test")),
        RAW,
    )
    yield Scenario(
        "split/listgroup-bytewise",
        script(
            step(
                "LISTGROUP alt.test",
                bytewise(ml(b"211 3 1 3 alt.test", [b"1", b"2", b"3"])),
            )
        ),
        lambda c: list(c.listgroup("alt.test")),
        Ok([1, 2, 3]),
        RAW,
    )
    yield Scenario(
        "split/hdr-bytewise",
        script(
            step("HDR Subject 1-2", bytewise(ml(b"225 hdr", [b"1 one", b"2 .two"])))
        ),
        lambda c: list(c.hdr("Subject", (1, 2))),
        Ok([(1, "one"), (2, ".two")]),
        RAW,
    )
    yield Scenario(
        "split/list-active-bytewise",
        script(
            step(
                "LIST ACTIVE",
                bytewise(ml(b"215 list", [b"alt.a 3 1 y", b"alt.b 0 1 m"])),
            )
        ),
        lambda c: [(g.name, g.low, g.high, g.status) for g in c.list_active()],
        Ok([("alt.a", 1, 3, "y"), ("alt.b", 1, 0, "m")]),
        RAW,
    )
    yield Scenario(
        "split/post-bytewise",
        script(
            step("POST", bytewise(b"340 send\r\n")),
            step(None, bytewise(b"240 ok <p@x>\r\n"), read_dot=True),
        ),
        lambda c: c.post(HEADERS, b"x\n.dot\n..\n.\n"),
        Ok("<p@x>"),
        RAW,
        server_check=article_is(
            [
                "Subject: s",
                "From: a@b",
                "Newsgroups: alt.test",
                "",
                "x",
                "..dot",
                "...",
                "..",
                ".",
            ]
        ),
    )
    drip: list[tuple[str, Action, Expect]] = [
        ("ARTICLE", lambda c: c.article("<1@test>")[2], Ok(STORE_BODY_BYTES)),
        ("BODY", lambda c: c.body("<1@test>"), Ok(STORE_BODY_BYTES)),
        ("HEAD", lambda c: c.head("<1@test>")["Subject"], Ok("article 1")),
        (
            "OVER",
            in_group(lambda c: list(c.over((1, 3)))),
            Ok(pred=store_over_check, desc="3 rows"),
        ),
        (
            "LIST",
            lambda c: sorted(g.name for g in c.list_active()),
            Ok(["alt.empty", "alt.test"]),
        ),
        ("GROUP", lambda c: c.group("alt.test"), Ok((3, 1, 3, "alt.test"))),
    ]
    for cmd, act, exp in drip:
        yield Scenario(
            f"split/store-drip-{cmd.lower()}",
            store_cfg(faults={"drip_ms": {cmd: 0.2}}),
            act,
            exp,
            probe="store",
            deadline=40,
        )
    yield Scenario(
        "split/store-linedelay-over-under-timeout",
        store_cfg(faults={"line_delay_ms": {"OVER": 400}}),
        in_group(lambda c: list(c.over((1, 3)))),
        Ok(pred=store_over_check, desc="3 rows"),
        {"timeout": 1},
        probe="store",
    )


def code_scenarios() -> Iterator[Scenario]:
    """Unexpected status codes, also multi-line ones."""
    blk = [b"x", b".y"]
    art = [b"S: a", b"", b"b"]
    reply_error = Err(nntp.NNTPReplyError, state="sync")
    data_error = Err(nntp.NNTPDataError, state="sync")
    cases: list[tuple[str, str, bytes, Action, Expect]] = [
        ("group-200", "GROUP g", b"200 ok\r\n", lambda c: c.group("g"), reply_error),
        (
            "group-211-nofields",
            "GROUP g",
            b"211\r\n",
            lambda c: c.group("g"),
            data_error,
        ),
        (
            "group-211-badnum",
            "GROUP g",
            b"211 x 1 2 g\r\n",
            lambda c: c.group("g"),
            data_error,
        ),
        (
            "article-222-block",
            "ARTICLE 1",
            ml(b"222 1 <a@b>", blk),
            lambda c: c.article(1),
            reply_error,
        ),
        (
            "article-223",
            "ARTICLE 1",
            b"223 1 <a@b>\r\n",
            lambda c: c.article(1),
            reply_error,
        ),
        (
            "article-220-badnum",
            "ARTICLE 1",
            ml(b"220 x <a@b>", art),
            lambda c: c.article(1),
            data_error,
        ),
        (
            "body-220-block",
            "BODY 1",
            ml(b"220 1 <a@b>", art),
            lambda c: c.body(1),
            reply_error,
        ),
        (
            "body-221-block",
            "BODY 1",
            ml(b"221 1 <a@b>", [b"S: a"]),
            lambda c: c.body(1),
            reply_error,
        ),
        ("body-199", "BODY 1", b"199 hm\r\n", lambda c: c.body(1), reply_error),
        ("body-381", "BODY 1", b"381 more\r\n", lambda c: c.body(1), reply_error),
        (
            "head-222-block",
            "HEAD 1",
            ml(b"222 1 <a@b>", blk),
            lambda c: c.head(1),
            reply_error,
        ),
        ("head-223", "HEAD 1", b"223 1 <a@b>\r\n", lambda c: c.head(1), reply_error),
        (
            "stat-220-block",
            "STAT 1",
            ml(b"220 1 <a@b>", art),
            lambda c: c.stat(1),
            reply_error,
        ),
        ("stat-223-nomsgid", "STAT 1", b"223 1\r\n", lambda c: c.stat(1), data_error),
        (
            "stat-223-badnum",
            "STAT 1",
            b"223 abc <a@b>\r\n",
            lambda c: c.stat(1),
            data_error,
        ),
        (
            "hdr-221-block",
            "HDR Subject 1",
            ml(b"221 x", [b"1 a"]),
            lambda c: list(c.hdr("Subject", 1)),
            reply_error,
        ),
        (
            "xhdr-225-block",
            "XHDR Subject 1",
            ml(b"225 x", [b"1 a"]),
            lambda c: list(c.xhdr("Subject", 1)),
            reply_error,
        ),
        (
            "list-231-block",
            "LIST ACTIVE",
            ml(b"231 x", [b"a 1 1 y"]),
            lambda c: list(c.list_active()),
            reply_error,
        ),
        (
            "list-200",
            "LIST ACTIVE",
            b"200 x\r\n",
            lambda c: list(c.list_active()),
            reply_error,
        ),
        (
            "list-282-block",
            "LIST ACTIVE",
            ml(b"282 x", [b"a 1 1 y"]),
            lambda c: list(c.list_active()),
            reply_error,
        ),
        (
            "listgroup-215-block",
            "LISTGROUP g",
            ml(b"215 x", [b"1"]),
            lambda c: list(c.listgroup("g")),
            reply_error,
        ),
        (
            "caps-215-block",
            "CAPABILITIES",
            ml(b"215 x", [b"VERSION 2"]),
            lambda c: list(c.capabilities()),
            reply_error,
        ),
        (
            "help-101-block",
            "HELP",
            ml(b"101 x", [b"VERSION 2"]),
            lambda c: c.help(),
            reply_error,
        ),
        (
            "newnews-231-block",
            "NEWNEWS .*",
            ml(b"231 x", [b"alt.a 1 1 y"]),
            lambda c: list(c.newnews("*", datetime(2020, 1, 1, tzinfo=UTC))),
            reply_error,
        ),
        ("date-garbage", "DATE", b"111 garbage\r\n", lambda c: c.date(), data_error),
        ("date-211", "DATE", b"211 1 1 1 g\r\n", lambda c: c.date(), reply_error),
        (
            "mode-reader-202",
            "MODE READER",
            b"202 hm\r\n",
            lambda c: c.mode_reader(),
            reply_error,
        ),
        (
            "compress-200",
            "COMPRESS DEFLATE",
            b"200 hm\r\n",
            lambda c: c.compress(),
            reply_error,
        ),
        (
            "xfeature-200",
            "XFEATURE COMPRESS GZIP",
            b"200 hm\r\n",
            lambda c: c.xfeature_compress_gzip(),
            reply_error,
        ),
        (
            "post-240-first",
            "POST",
            b"240 ok\r\n",
            lambda c: c.post(HEADERS, b"x"),
            reply_error,
        ),
        (
            "post-335-first",
            "POST",
            b"335 ok\r\n",
            lambda c: c.post(HEADERS, b"x"),
            reply_error,
        ),
        (
            "ihave-340-first",
            "IHAVE <i@x>",
            b"340 ok\r\n",
            lambda c: c.ihave("<i@x>", HEADERS, b"x"),
            reply_error,
        ),
        ("quit-200", "QUIT", b"200 hm\r\n", lambda c: c.quit(), reply_error),
        (
            "authinfo-user-250",
            "AUTHINFO USER u",
            b"250 hm\r\n",
            lambda c: c.login("u", "p"),
            reply_error,
        ),
    ]
    for name, line, reply, act, exp in cases:
        yield Scenario(
            f"code/{name}",
            script(step(line, reply)),
            act,
            exp,
            RAW,
            server_check=no_mismatch,
        )
    for name, second in [
        ("post-340-then-340", b"340 again\r\n"),
        ("post-340-then-235", b"235 ok\r\n"),
    ]:
        yield Scenario(
            f"code/{name}",
            script(step("POST", b"340 send\r\n"), step(None, second, read_dot=True)),
            lambda c: c.post(HEADERS, b"x"),
            reply_error,
            RAW,
            server_check=no_mismatch,
        )
    yield Scenario(
        "code/authinfo-user-281",
        script(step("AUTHINFO USER u", b"281 ok\r\n")),
        lambda c: c.login("u", "p"),
        Ok(None),
        RAW,
        server_check=no_lines("AUTHINFO PASS"),
    )
    yield Scenario(
        "code/authinfo-user-215-block",
        script(step("AUTHINFO USER u", ml(b"215 what", [b"111 19990101000000"]))),
        lambda c: c.login("u", "p"),
        reply_error,
        RAW,
    )
    # A server bug: the client must time out and mark the connection broken
    yield Scenario(
        "code/listgroup-211-without-block",
        script(step("LISTGROUP g", b"211 2 1 2 g\r\n")),
        lambda c: list(c.listgroup("g")),
        Err(*CLEAN_ERRORS, state="broken"),
        {"reader": False, "timeout": 1},
        probe=None,
    )


def leftover_scenarios() -> Iterator[Scenario]:
    """Unsolicited data after a reply."""
    yield Scenario(
        "leftover/second-status-same-packet",
        script(
            step("GROUP alt.a", b"211 1 1 1 alt.a\r\n211 9 9 9 alt.b\r\n"),
            step("GROUP alt.c", b"411 no such group\r\n"),
        ),
        lambda c: (c.group("alt.a"), attempt(lambda: c.group("alt.c"))),
        Clean(
            pred=lambda v: v[1] != (9, 9, 9, "alt.b"),
            desc="the second group() must not return the stale 211 for alt.b",
        ),
        RAW,
        probe=None,
        known=UNREAD_DATA,
    )
    yield Scenario(
        "leftover/status-after-terminator-same-packet",
        script(step("BODY 1", ml(b"222 1 <a@b>", [b"x"]) + STALE_DATE)),
        lambda c: (c.body(1), attempt(lambda: c.date().year)),
        Clean(
            pred=lambda v: v[1] != 2099,
            desc="date() must not return the unsolicited 2099 line",
        ),
        RAW,
        probe=None,
        known=UNREAD_DATA,
    )
    yield Scenario(
        "leftover/extra-line-after-greeting",
        script(greeting=GREETING + STALE_DATE),
        lambda c: c.date().year,
        Clean(
            pred=lambda v: v != 2099,
            desc="date() must not return the 2099 line sent with the greeting",
        ),
        RAW,
        probe=None,
        known=UNREAD_DATA,
    )
    yield Scenario(
        "leftover/crlf-after-terminator",
        script(step("BODY 1", ml(b"222 1 <a@b>", [b"x"]) + CRLF)),
        lambda c: c.body(1),
        Clean(pred=lambda v: v == b"x\r\n"),
        RAW,
    )


def drop_scenarios() -> Iterator[Scenario]:
    """The server closes or resets the connection."""
    broken = Err(*CLEAN_ERRORS, state="broken")
    cmds: list[tuple[str, Action]] = [
        ("ARTICLE", lambda c: c.article("<1@test>")),
        ("BODY", lambda c: c.body("<1@test>")),
        ("HEAD", lambda c: c.head("<1@test>")),
        ("OVER", in_group(lambda c: list(c.over((1, 3))))),
        ("LIST", lambda c: list(c.list_active())),
        ("LISTGROUP", lambda c: list(c.listgroup("alt.test"))),
        ("CAPABILITIES", lambda c: list(c.capabilities())),
    ]
    for cmd, act in cmds:
        for after in (0, 1, 3):
            yield Scenario(
                f"drop/store-{cmd.lower()}-after-{after}",
                store_cfg(faults={"drop_in_multiline": {cmd: after}}),
                act,
                broken,
                probe="store",
            )
    yield Scenario(
        "drop/store-close-after-2",
        store_cfg(faults={"close_after": 2}),
        in_group(lambda c: c.body(1)),
        broken,
        probe="store",
    )
    raw_drops: list[tuple[str, JSON]] = [
        ("mid-status-line", step("BODY 1", b"222 1 <a", then="close")),
        ("after-status-line", step("BODY 1", b"222 1 <a@b>\r\n", then="close")),
        ("mid-block-line", step("BODY 1", b"222 1 <a@b>\r\nab", then="close")),
        (
            "before-terminator-lf",
            step("BODY 1", b"222 1 <a@b>\r\nab\r\n.\r", then="close"),
        ),
        (
            "rst-mid-block",
            step("BODY 1", b"222 1 <a@b>\r\nab\r\n", sleep=50, then="rst"),
        ),
    ]
    for name, st in raw_drops:
        yield Scenario(
            f"drop/{name}",
            script(st, probe=False),
            lambda c: c.body(1),
            broken,
            RAW,
            probe=None,
        )
    yield Scenario(
        "drop/no-greeting-close",
        script(step(None, b"", then="close"), greeting=None, probe=False),
        None,
        Err(*CLEAN_ERRORS),
        RAW,
        probe=None,
    )
    yield Scenario(
        "drop/endless-body",
        store_cfg(faults={"endless": {"BODY": True}}, deadline_ms=2000),
        lambda c: c.body("<1@test>"),
        broken,
        {"timeout": 5},
        probe="store",
        max_s=10,
    )


def timeout_reconnect(c: nntp.NNTPClient) -> object:
    with contextlib.suppress(OSError):
        c.body("<1@test>")
    try:
        c.date()
    except nntp.NNTPSyncError:
        pass
    else:
        return "no NNTPSyncError after timeout"
    c.reconnect()
    return c.group("alt.test")


def timeout_scenarios() -> Iterator[Scenario]:
    """Slow servers and the client timeout."""
    timed_out = Err(OSError, state="broken")
    yield Scenario(
        "timeout/store-delay-body",
        store_cfg(faults={"delay_ms": {"BODY": 2500}}),
        lambda c: c.body("<1@test>"),
        timed_out,
        {"timeout": 1},
        probe="store",
        max_s=6,
    )
    yield Scenario(
        "timeout/store-then-reconnect",
        store_cfg(faults={"delay_ms": {"BODY": 2500}}),
        timeout_reconnect,
        Clean(pred=lambda v: v == (3, 1, 3, "alt.test")),
        {"timeout": 1},
        probe="store",
        max_s=10,
    )
    yield Scenario(
        "timeout/no-greeting",
        store_cfg(no_greeting=True),
        None,
        Err(OSError),
        {"timeout": 1},
        probe=None,
        max_s=5,
    )
    yield Scenario(
        "timeout/drip-under-timeout",
        store_cfg(faults={"drip_ms": {"DATE": 30}}),
        lambda c: c.date().year > 2000,
        Ok(True),
        {"timeout": 1},
        probe="store",
    )
    yield Scenario(
        "timeout/mid-block-stall",
        script(step("BODY 1", b"222 1 <a@b>\r\nab\r\n", chunk(b".\r\n", gap=2500))),
        lambda c: c.body(1),
        timed_out,
        {"reader": False, "timeout": 1},
        probe=None,
        max_s=6,
    )
    yield Scenario(
        "timeout/after-post-article",
        script(
            step("POST", b"340 go\r\n"),
            step(None, chunk(b"240 ok\r\n", gap=2500), read_dot=True),
        ),
        lambda c: c.post({"Subject": "s"}, b"x"),
        timed_out,
        {"reader": False, "timeout": 1},
        probe=None,
        max_s=6,
    )
    yield Scenario(
        "timeout/tls-handshake-stall",
        {"mode": "store", "raw_stall": True},
        None,
        Err(OSError),
        {"timeout": 1, "use_ssl": True, "ca": True},
        probe=None,
        max_s=5,
    )


def tls_scenarios() -> Iterator[Scenario]:
    """Implicit TLS and STARTTLS."""
    broken = Err(*CLEAN_ERRORS, state="broken")
    yield Scenario(
        "tls/implicit-article",
        store_cfg(tls=True),
        lambda c: c.article("<1@test>")[2],
        Ok(STORE_BODY_BYTES),
        TLS,
        probe="store",
    )
    yield Scenario(
        "tls/implicit-untrusted-cert",
        store_cfg(tls=True),
        None,
        Err(OSError),
        {"use_ssl": True},
        probe=None,
        server_check=no_lines("MODE"),
    )
    yield Scenario(
        "tls/implicit-drip-body",
        store_cfg(tls=True, faults={"drip_ms": {"BODY": 0.2}}),
        lambda c: c.body("<1@test>"),
        Ok(STORE_BODY_BYTES),
        TLS,
        probe="store",
        deadline=40,
    )
    yield Scenario(
        "tls/implicit-drop-body",
        store_cfg(tls=True, faults={"drop_in_multiline": {"BODY": 2}}),
        lambda c: c.body("<1@test>"),
        broken,
        TLS,
        probe="store",
    )
    yield Scenario(
        "tls/implicit-close-notify-mid-block",
        script(
            step("BODY 1", b"222 1 <a@b>\r\nab\r\n", then="close"),
            tls=True,
            probe=False,
        ),
        lambda c: c.body(1),
        broken,
        {**TLS, "reader": False},
        probe=None,
    )
    yield Scenario(
        "tls/implicit-plaintext-server",
        script(),
        None,
        Err(OSError),
        {**TLS, "timeout": 2},
        probe=None,
        max_s=6,
    )
    yield Scenario(
        "tls/starttls-ok",
        script(step("STARTTLS", b"382 go\r\n", then="starttls")),
        None,
        Ok("connected"),
        STARTTLS,
    )
    yield Scenario(
        "tls/starttls-382-bytewise",
        script(step("STARTTLS", bytewise(b"382 go ahead\r\n"), then="starttls")),
        None,
        Ok("connected"),
        STARTTLS,
    )
    yield Scenario(
        "tls/starttls-data-after-382-same-packet",
        script(
            step("STARTTLS", b"382 go\r\n" + STALE_DATE, then="starttls"), probe=False
        ),
        None,
        Err(nntp.NNTPProtocolError),
        STARTTLS,
        probe=None,
        server_check=no_mismatch,
    )
    yield Scenario(
        "tls/starttls-data-after-382-next-packet",
        script(
            step("STARTTLS", b"382 go\r\n", chunk(STALE_DATE, gap=50), then="starttls"),
            probe=False,
        ),
        None,
        Err(OSError),
        STARTTLS,
        probe=None,
    )
    no_fallback = all_of(no_lines("AUTHINFO"), no_lines("MODE"))
    login = {**STARTTLS, **CREDS, "reader": True}
    refusals: list[tuple[str, bytes, Expect, ServerCheck]] = [
        (
            "580-no-fallback",
            b"580 no tls\r\n",
            Err(nntp.NNTPPermanentError),
            no_fallback,
        ),
        ("200-no-fallback", b"200 sure\r\n", Err(nntp.NNTPReplyError), no_fallback),
        (
            "480-no-creds-in-clear",
            b"480 auth first\r\n",
            Err(nntp.NNTPTemporaryError),
            no_lines("AUTHINFO", "before TLS"),
        ),
    ]
    for name, reply, exp, check in refusals:
        yield Scenario(
            f"tls/starttls-{name}",
            script(step("STARTTLS", reply), probe=False),
            None,
            exp,
            login,
            probe=None,
            server_check=check,
        )
    yield Scenario(
        "tls/starttls-382-then-close",
        script(step("STARTTLS", b"382 go\r\n", then="close"), probe=False),
        None,
        Err(*CLEAN_ERRORS),
        STARTTLS,
        probe=None,
    )
    yield Scenario(
        "tls/starttls-login-inside",
        script(
            step("STARTTLS", b"382 go\r\n", then="starttls"),
            step("MODE READER", b"480 login\r\n"),
            step("AUTHINFO USER u", b"381 pass\r\n"),
            step("AUTHINFO PASS p", b"281 ok\r\n"),
        ),
        None,
        Ok("connected"),
        login,
        server_check=max_lines("MODE READER", 1),
    )
    yield Scenario(
        "tls/implicit-compress",
        store_cfg(tls=True, store={"compress": True}),
        compressed(lambda c: c.body("<1@test>")),
        Ok(STORE_BODY_BYTES),
        TLS,
        probe="store",
    )


def compress_store_basic(c: nntp.NNTPClient) -> object:
    c.compress()
    body = c.article("<1@test>")[2]
    c.group("alt.test")
    list(c.over((1, 3)))
    c.post(HEADERS, b".x\n")
    return body


def compress_store_scenarios() -> Iterator[Scenario]:
    """COMPRESS DEFLATE with the fake server's Store."""
    yield Scenario(
        "compress/store-basic",
        store_cfg(store={"compress": True}),
        compress_store_basic,
        Ok(STORE_BODY_BYTES),
        probe="store",
    )
    yield Scenario(
        "compress/store-drip-over",
        store_cfg(store={"compress": True}, faults={"drip_ms": {"OVER": 0.3}}),
        compressed(in_group(lambda c: list(c.over((1, 3))))),
        Ok(pred=store_over_check, desc="3 rows"),
        probe="store",
        deadline=40,
    )
    yield Scenario(
        "compress/store-drop-article",
        store_cfg(
            store={"compress": True}, faults={"drop_in_multiline": {"ARTICLE": 2}}
        ),
        compressed(lambda c: c.article("<1@test>")),
        Err(*CLEAN_ERRORS, state="broken"),
        probe="store",
    )
    yield Scenario(
        "compress/store-480-after-compress",
        store_cfg(
            store={"compress": True, "user": "u", "pass": "p"},
            faults={"override": {"GROUP": ["480 login again"]}},
        ),
        compressed(lambda c: c.group("alt.test")),
        Err(nntp.NNTPTemporaryError, state="sync", match="after COMPRESS"),
        CREDS,
        probe="store",
        server_check=no_lines_after("COMPRESS DEFLATE", "AUTHINFO"),
    )
    # A half-broken server: only a bounded, clean failure is required
    yield Scenario(
        "compress/store-output-only",
        store_cfg(store={"compress_output_only": True}),
        compressed(lambda c: c.date()),
        Clean(),
        {"timeout": 2},
        probe="store",
    )


def compress_raw(*steps: JSON) -> JSON:
    """A script after "COMPRESS DEFLATE" -> 206, without the probe."""
    return script(
        step("COMPRESS DEFLATE", b"206 ok\r\n", then="inflate"), *steps, probe=False
    )


def compress_raw_scenarios() -> Iterator[Scenario]:
    """COMPRESS DEFLATE with byte-exact compressed replies."""
    date_ok = compressed(lambda c: c.date().timetuple()[:6])
    broken = Err(nntp.NNTPDataError, state="broken")
    z = Deflate()
    first = b"206 compressing\r\n"
    yield Scenario(
        "compress/206-plain",
        script(
            step("COMPRESS DEFLATE", first, then="inflate"),
            step("DATE", z.sync(PROBE_REPLY)),
            probe=False,
        ),
        date_ok,
        Ok(PROBE_TUPLE),
        RAW,
        probe=None,
    )
    z = Deflate()
    data = first + z.sync(b"")
    yield Scenario(
        "compress/206-empty-flush-same-packet",
        script(
            step("COMPRESS DEFLATE", data, then="inflate"),
            step("DATE", z.sync(PROBE_REPLY)),
            probe=False,
        ),
        date_ok,
        Ok(PROBE_TUPLE),
        RAW,
        probe=None,
    )
    z = Deflate()
    data = first + z.sync(b"")
    yield Scenario(
        "compress/206-split-then-flush",
        script(
            step("COMPRESS DEFLATE", data[:5], chunk(data[5:], gap=20), then="inflate"),
            step("DATE", z.sync(PROBE_REPLY)),
            probe=False,
        ),
        date_ok,
        Ok(PROBE_TUPLE),
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/reply-bytewise",
        compress_raw(step("DATE", bytewise(Deflate().sync(PROBE_REPLY)))),
        date_ok,
        Ok(PROBE_TUPLE),
        RAW,
        probe=None,
    )
    # INN flushes with Z_PARTIAL_FLUSH, so a reply has no sync marker
    yield Scenario(
        "compress/partial-flush-like-inn",
        compress_raw(step("DATE", Deflate().partial(PROBE_REPLY))),
        date_ok,
        Ok(PROBE_TUPLE),
        {"reader": False, "timeout": 2},
        probe=None,
    )
    z = Deflate()
    yield Scenario(
        "compress/partial-flush-body",
        compress_raw(
            step("BODY 1", z.partial(ml(b"222 1 <a@b>", BODY_LINES))),
            step("DATE", z.partial(PROBE_REPLY)),
        ),
        compressed(lambda c: (c.body(1, decode=False), c.date().timetuple()[:6])),
        Ok((BODY_BYTES, PROBE_TUPLE)),
        {"reader": False, "timeout": 2},
        probe=None,
    )
    data = Deflate().sync(PROBE_REPLY)  # ends in the 00 00 ff ff sync marker
    yield Scenario(
        "compress/sync-marker-split",
        compress_raw(step("DATE", data[:-3], chunk(data[-3:], gap=30))),
        date_ok,
        Ok(PROBE_TUPLE),
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/corrupt-reply",
        compress_raw(step("DATE", b"\xff\xff\xff\xffgarbage\r\n")),
        date_ok,
        broken,
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/corrupt-tail-after-206",
        script(
            step("COMPRESS DEFLATE", b"206 ok\r\n\xff\xff\xff\xffjunk", then="inflate"),
            probe=False,
        ),
        lambda c: c.compress(),
        broken,
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/data-after-stream-end",
        compress_raw(step("DATE", Deflate().finish(PROBE_REPLY) + STALE_DATE)),
        date_ok,
        broken,
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/stream-finished-exactly",
        compress_raw(step("DATE", Deflate().finish(PROBE_REPLY))),
        date_ok,
        Clean(pred=lambda v: v == PROBE_TUPLE),
        RAW,
        probe=None,
    )
    big = [b"%06d %s" % (i, b"." * (i % 7) + b"x" * 60) for i in range(20000)]
    z = Deflate()
    yield Scenario(
        "compress/big-body-split",
        compress_raw(
            step(
                "BODY 1", chunk(z.sync(ml(b"222 1 <a@b>", big)), split=1000, sgap=0.05)
            ),
            step("DATE", z.sync(PROBE_REPLY)),
        ),
        compressed(
            lambda c: (c.body(1, decode=False) == joined(big), c.date().timetuple()[:6])
        ),
        Ok((True, PROBE_TUPLE)),
        RAW,
        probe=None,
        deadline=40,
    )
    yield Scenario(
        "compress/206-then-close",
        script(step("COMPRESS DEFLATE", b"206 ok\r\n", then="close"), probe=False),
        date_ok,
        Err(*CLEAN_ERRORS, state="broken"),
        RAW,
        probe=None,
    )
    yield Scenario(
        "compress/403",
        script(step("COMPRESS DEFLATE", b"403 not now\r\n")),
        lambda c: c.compress(),
        Err(nntp.NNTPTemporaryError, state="sync"),
        RAW,
    )


def gzip_xhdr(terminator: bool) -> Action:
    def act(c: nntp.NNTPClient) -> object:
        c.xfeature_compress_gzip(terminator)
        return list(c.xhdr("Subject", (1, 3)))

    return act


def gzip_scenarios() -> Iterator[Scenario]:
    """XFEATURE COMPRESS GZIP, with and without TERMINATOR."""
    rows = stuff([b"1 one", b"2 .two", b"3 three"])
    value = Ok([(1, "one"), (2, ".two"), (3, "three")])
    status = b"221 Header follows (COMPRESS=GZIP)\r\n"
    g = gzip_bytes(rows)
    gt = gzip_bytes(rows + b".\r\n")
    c = zlib.compressobj(wbits=31)
    head = c.compress(rows + b".\r\n") + c.flush(zlib.Z_SYNC_FLUSH)
    tail = c.flush(zlib.Z_FINISH)
    plain, term = "XFEATURE COMPRESS GZIP", "XFEATURE COMPRESS GZIP TERMINATOR"
    cases: list[tuple[str, str, list[bytes | JSON], Expect]] = [
        ("dot-outside-one-packet", plain, [g + b".\r\n"], value),
        ("dot-outside-next-packet", plain, [g, chunk(b".\r\n", gap=30)], value),
        ("trailer-split", plain, [g[:-4], chunk(g[-4:] + b".\r\n", gap=30)], value),
        ("dot-split", plain, [g + b".", chunk(b"\r\n", gap=30)], value),
        ("bytewise", plain, [bytewise(g + b".\r\n")], value),
        ("terminator-inside", term, [gt], value),
        (
            "terminator-inside-trailer-split",
            term,
            [gt[:-4], chunk(gt[-4:], gap=30)],
            value,
        ),
        ("terminator-inside-bytewise", term, [bytewise(gt)], value),
        ("terminator-requested-dot-outside", term, [g + b".\r\n"], value),
        ("empty", plain, [gzip_bytes(b"") + b".\r\n"], Ok([])),
        (
            "corrupt",
            plain,
            [b"\x1f\x8b\x08\x00garbage-garbage\r\n.\r\n"],
            Err(nntp.NNTPDataError, state="broken"),
        ),
    ]
    for name, feature, chunks, exp in cases:
        yield Scenario(
            f"gzip/{name}",
            script(
                step(re.escape(feature), b"290 ok\r\n"),
                step("XHDR Subject 1-3", status, *chunks),
            ),
            gzip_xhdr(feature == term),
            exp,
            RAW,
        )
    yield Scenario(
        "gzip/terminator-inside-trailer-late",
        script(
            step(re.escape(term), b"290 ok\r\n"),
            step("XHDR Subject 1-3", status, head, chunk(tail, gap=2500)),
        ),
        gzip_xhdr(terminator=True),
        value,
        {"reader": False, "timeout": 1},
        max_s=6,
        known="with TERMINATOR, the reply waits for the end of the gzip stream "
        "after the terminator",
    )
    yield Scenario(
        "gzip/drop-mid-stream",
        script(
            step(plain, b"290 ok\r\n"),
            step("XHDR Subject 1-3", status, g[: len(g) // 2], then="close"),
            probe=False,
        ),
        gzip_xhdr(terminator=False),
        Err(*CLEAN_ERRORS, state="broken"),
        RAW,
        probe=None,
    )


def dot_line_payload() -> tuple[int, list[bytes]]:
    """XZVER lines of which one yEnc line starts with "." (dot-stuffed)."""
    # Searched, as the compressed bytes depend on the zlib build
    for seed in range(200):
        rows = [r.replace(b"subj", b"s%d" % seed) + CRLF for r in OVER_ROWS]
        for line in range(8, 64):
            lines = xz_payload(rows, line=line)
            if any(ln.startswith(b".") for ln in lines[1:-1]):
                return seed, lines
    msg = "found no yEnc line that starts with a dot"
    raise RuntimeError(msg)


def xzver_scenarios() -> Iterator[Scenario]:
    """XZVER and XZHDR: yEnc-encoded DEFLATE."""
    rows = [r + CRLF for r in OVER_ROWS]
    good = xz_payload(rows)
    two_rows = Ok(pred=over_check, desc="2 rows")
    seed, dotted = dot_line_payload()
    comp = zlib.compressobj(wbits=-15)
    junk = comp.compress(b"".join(rows)) + comp.flush() + b"TRAILING-JUNK"
    cases: list[tuple[str, list[bytes], Expect, bool, str]] = [
        ("finished", good, two_rows, False, ""),
        ("finished-bytewise", good, two_rows, True, ""),
        ("finished-short-lines", xz_payload(rows, line=7), two_rows, False, ""),
        (
            "sync-flushed-not-finished",
            xz_payload(rows, finish=False),
            two_rows,
            False,
            (
                "XZVER rejects a complete DEFLATE stream that is sync-flushed, "
                "not finished"
            ),
        ),
        (
            "last-row-without-crlf",
            xz_payload([*rows[:-1], OVER_ROWS[-1]]),
            two_rows,
            False,
            "XZVER drops a last row that has no CRLF",
        ),
        ("bad-crc", xz_payload(rows, crc=1), Err(nntp.NNTPDataError), False, ""),
        (
            "bad-size",
            [
                good[0].replace(b"size=", b"size=9"),
                *good[1:-1],
                good[-1].replace(b"size=", b"size=9"),
            ],
            Err(nntp.NNTPDataError),
            False,
            "",
        ),
        (
            "zlib-corrupt",
            [good[0], b"garbage-not-deflate!", good[-1]],
            Err(nntp.NNTPDataError, state="broken"),
            False,
            "",
        ),
        ("missing-trailer", good[:-1], Err(nntp.NNTPDataError), False, ""),
        ("empty", [], Ok([]), False, ""),
        (
            "dot-stuffed-yenc-line",
            dotted,
            Ok(
                pred=lambda v: len(v) == 2 and v[1][1]["Subject"] == f".dotted s{seed}",
                desc="2 rows",
            ),
            False,
            "",
        ),
        # Junk after the end of the DEFLATE stream is accepted (observed)
        ("data-after-deflate-end", yenc_of(junk), Clean(pred=over_check), False, ""),
    ]
    for name, lines, exp, slow, known in cases:
        reply = ml(b"224 compressed data follows", lines)
        yield Scenario(
            f"xzver/{name}",
            script(
                step("LIST OVERVIEW.FMT", OVERFMT),
                step("XZVER 1-2", bytewise(reply, 0.1) if slow else reply),
            ),
            lambda c: list(c.xzver((1, 2))),
            exp,
            RAW,
            known=known,
        )
    yield Scenario(
        "xzver/xzhdr-finished",
        script(
            step(
                "XZHDR Subject 1-2",
                ml(
                    b"221 compressed headers follow",
                    xz_payload([b"1 one\r\n", b"2 two\r\n"]),
                ),
            )
        ),
        lambda c: list(c.xzhdr("Subject", (1, 2))),
        Ok([(1, "one"), (2, "two")]),
        RAW,
    )


def body_action(decode: bool | None) -> Action:
    return lambda c: c.body(1, decode=decode)


def post_action(body: bytes) -> Action:
    return lambda c: c.post(HEADERS, body)


def yenc_error(data: bytes) -> Err:
    """NNTPYEncError that still carries the decoded data, in sync."""
    return Err(
        nntp.NNTPYEncError,
        state="sync",
        pred=lambda e: isinstance(e, nntp.NNTPYEncError) and e.data == data,
    )


def yenc_scenarios() -> Iterator[Scenario]:
    """yEnc bodies."""
    data = b"\x04" + bytes(range(256)) * 4  # the first encoded line starts with "."
    data2 = bytes(range(255, -1, -1)) * 2
    block = yenc_block(data)
    esc = bytes([0xD6])  # encodes to NUL, escaped as "=@"
    eblock = yenc_block(esc * 300)
    # split between "=" and the escaped byte
    esc_reply = ml(b"222 1 <y@x>", eblock)
    esc_at = esc_reply.index(eblock[1]) + eblock[1].index(b"=") + 1
    cases: list[tuple[str, list[bytes], Expect, bool | None, str]] = [
        ("single", block, Ok(data), None, ""),
        (
            "multipart-part1",
            yenc_block(data, part=(1, 2), total_size=len(data) + len(data2)),
            Ok(data),
            None,
            "",
        ),
        ("crc-mismatch", yenc_block(data, crc=0x1234), yenc_error(data), None, ""),
        (
            "size-mismatch",
            yenc_block(data, size=len(data) + 1),
            yenc_error(data),
            None,
            "",
        ),
        (
            "text-before-autodetect",
            [b"hello", b"", *block],
            Ok(joined([b"hello", b"", *block])),
            None,
            "",
        ),
        ("text-before-decode-true", [b"hello", b"", *block], Ok(data), True, ""),
        ("text-after", [*block, b"", b"-- ", b"sig"], Ok(data), None, ""),
        ("blank-lines-before", [b"", b"", *block], Ok(data), None, ""),
        ("missing-yend", block[:-1], yenc_error(data), None, ""),
        (
            "two-blocks",
            block + yenc_block(data2, name="g.bin"),
            Ok(data + data2),
            None,
            "",
        ),
        (
            "stray-yend-after-block",
            [*block, b"=yend size=0"],
            Ok(data),
            None,
            "a =yend line after a complete block raises NNTPYEncError",
        ),
        (
            "decode-true-no-ybegin",
            [b"plain text"],
            Err(nntp.NNTPYEncError, state="sync"),
            True,
            "",
        ),
    ]
    for name, lines, exp, decode, known in cases:
        yield Scenario(
            f"yenc/{name}",
            script(step("BODY 1", ml(b"222 1 <y@x>", lines))),
            body_action(decode),
            exp,
            RAW,
            known=known,
        )
    yield Scenario(
        "yenc/single-bytewise",
        script(step("BODY 1", bytewise(ml(b"222 1 <y@x>", block), 0.05))),
        lambda c: c.body(1),
        Ok(data),
        RAW,
    )
    yield Scenario(
        "yenc/article-single",
        script(step("ARTICLE 1", ml(b"220 1 <y@x>", [b"Subject: y", b"", *block]))),
        lambda c: c.article(1)[2],
        Ok(data),
        RAW,
    )
    yield Scenario(
        "yenc/escape-split-across-packets",
        script(step("BODY 1", esc_reply[:esc_at], chunk(esc_reply[esc_at:], gap=30))),
        lambda c: c.body(1),
        Ok(esc * 300),
        RAW,
    )


def post_scenarios() -> Iterator[Scenario]:
    """POST."""
    big = (b"y" * 70 + b"\n") * 60000
    broken = Err(*CLEAN_ERRORS, state="broken")
    yield Scenario(
        "post/441-after-article",
        script(
            step("POST", b"340 go\r\n"), step(None, b"441 rejected\r\n", read_dot=True)
        ),
        lambda c: c.post(HEADERS, b"x"),
        Err(nntp.NNTPTemporaryError, state="sync"),
        RAW,
        server_check=no_mismatch,
    )
    yield Scenario(
        "post/440-not-allowed",
        script(step("POST", b"440 no posting\r\n")),
        lambda c: c.post(HEADERS, b"x"),
        Err(nntp.NNTPTemporaryError, state="sync"),
        RAW,
        server_check=no_mismatch,
    )
    drops: list[tuple[str, bytes, str]] = [
        ("drop-mid-article-small", b"x\n" * 10, "close"),
        ("drop-mid-article-large", big, "close"),
        ("rst-mid-article-large", big, "rst"),
    ]
    for name, body, then in drops:
        yield Scenario(
            f"post/{name}",
            script(
                step("POST", b"340 go\r\n"),
                step(None, read_dot=True, dot_max=2, then=then),
                probe=False,
            ),
            post_action(body),
            broken,
            RAW,
            probe=None,
        )
    yield Scenario(
        "post/early-441-then-close",
        script(step("POST", b"340 go\r\n441 too big\r\n", then="close"), probe=False),
        lambda c: c.post(HEADERS, big),
        Err(*CLEAN_ERRORS),
        RAW,
        probe=None,
    )
    yield Scenario(
        "post/480-login-retry",
        script(
            step("POST", b"480 login\r\n"),
            step("AUTHINFO USER u", b"381 pass\r\n"),
            step("AUTHINFO PASS p", b"281 ok\r\n"),
            step("POST", b"340 go\r\n"),
            step(None, b"240 ok <z@x>\r\n", read_dot=True),
        ),
        lambda c: c.post(HEADERS, b"x"),
        Ok("<z@x>"),
        {"reader": False, **CREDS},
        server_check=no_mismatch,
    )
    yield Scenario(
        "post/store-ok",
        store_cfg(),
        lambda c: c.post(HEADERS | {"Message-ID": "<new@x>"}, b".a\n..b\n"),
        Ok("<new@x>"),
        probe="store",
        server_check=lambda s: (
            None
            if s["articles"]
            and [x for x in s["articles"][-1] if x != "."][-2:] == ["..a", "...b"]
            else f"article {s['articles']}"
        ),
    )


def ihave_defer(c: nntp.NNTPClient) -> object:
    try:
        c.ihave("<def@x>", HEADERS, b"b\n")
    except nntp.NNTPTemporaryError as e:
        if e.code != 436:
            raise
    c.ihave("<def@x>", HEADERS, b"b\n")
    return "accepted"


def ihave_scenarios() -> Iterator[Scenario]:
    """IHAVE, with the fake server's peer."""
    peer = {
        "mode": "peer",
        "peer": {
            "reject": {"<rej@x>": True},
            "have": {"<have@x>": True},
            "defer436": {"<def@x>": True},
        },
    }
    yield Scenario(
        "ihave/peer-ok",
        peer,
        lambda c: c.ihave("<new@x>", HEADERS, b"body\n"),
        Ok(None),
        RAW,
        probe=None,
    )
    yield Scenario(
        "ihave/peer-437-reject",
        peer,
        lambda c: c.ihave("<rej@x>", HEADERS, b"body\n"),
        Err(nntp.NNTPTemporaryError, match="437"),
        RAW,
        probe=None,
        server_check=lambda s: (
            None if len(s["articles"]) == 1 else "article not sent once"
        ),
    )
    yield Scenario(
        "ihave/peer-435-have",
        peer,
        lambda c: c.ihave("<have@x>", HEADERS, b"body\n"),
        Err(nntp.NNTPTemporaryError, match="435"),
        RAW,
        probe=None,
        server_check=lambda s: "article sent" if s["articles"] else None,
    )
    yield Scenario(
        "ihave/peer-436-then-ok", peer, ihave_defer, Ok("accepted"), RAW, probe=None
    )
    yield Scenario(
        "ihave/peer-auth-480-login",
        {"mode": "peer", "peer": {"user": "u", "pass": "p"}},
        lambda c: c.ihave("<a1@x>", HEADERS, b"b\n"),
        Ok(None),
        {"reader": False, **CREDS},
        probe=None,
    )
    yield Scenario(
        "ihave/peer-compress",
        {"mode": "peer", "peer": {"compress": True}},
        compressed(lambda c: c.ihave("<cz@x>", HEADERS, b".dot\nbody\n")),
        Ok(None),
        RAW,
        probe=None,
    )
    yield Scenario(
        "ihave/335-then-437",
        script(
            step("IHAVE <i@x>", b"335 send\r\n"),
            step(None, b"437 no\r\n", read_dot=True),
        ),
        lambda c: c.ihave("<i@x>", HEADERS, b"b\n"),
        Err(nntp.NNTPTemporaryError, state="sync"),
        RAW,
    )
    yield Scenario(
        "ihave/335-then-drop",
        script(
            step("IHAVE <i@x>", b"335 send\r\n"),
            step(None, read_dot=True, dot_max=1, then="close"),
            probe=False,
        ),
        lambda c: c.ihave("<i@x>", HEADERS, b"b\n" * 5),
        Err(*CLEAN_ERRORS, state="broken"),
        RAW,
        probe=None,
    )


def auth_scenarios() -> Iterator[Scenario]:
    """AUTHINFO USER/PASS."""
    auth = {"user": "u", "pass": "p"}
    yield Scenario(
        "auth/store-ok",
        store_cfg(store=auth),
        in_group(lambda c: c.body(1)),
        Ok(STORE_BODY_BYTES),
        CREDS,
        probe="store",
    )
    yield Scenario(
        "auth/store-no-creds",
        store_cfg(store=auth),
        lambda c: c.group("alt.test"),
        Err(nntp.NNTPTemporaryError, state="sync", match="480"),
        RAW,
        probe=None,
    )
    yield Scenario(
        "auth/store-bad-password",
        store_cfg(store=auth),
        lambda c: c.group("alt.test"),
        Err(nntp.NNTPReplyError, state="sync", match="481"),
        {"reader": False, "username": "u", "password": "x"},
        probe=None,
    )
    relogin: list[tuple[str, Action, Expect]] = [
        ("GROUP", lambda c: c.group("alt.test"), Ok((3, 1, 3, "alt.test"))),
        ("ARTICLE", lambda c: c.article("<1@test>")[2], Ok(STORE_BODY_BYTES)),
        (
            "OVER",
            in_group(lambda c: list(c.over((1, 3)))),
            Ok(pred=store_over_check, desc="3 rows"),
        ),
        ("LIST", lambda c: len(list(c.list_active())), Ok(2)),
        ("LISTGROUP", lambda c: list(c.listgroup("alt.test")), Ok([1, 2, 3])),
        (
            "POST",
            lambda c: c.post(HEADERS | {"Message-ID": "<p1@x>"}, b"x"),
            Ok("<p1@x>"),
        ),
    ]
    for cmd, act, exp in relogin:
        yield Scenario(
            f"auth/480-once-on-{cmd.lower()}",
            store_cfg(
                store=auth,
                faults={
                    "override": {cmd: ["480 login expired"]},
                    "override_times": {cmd: 1},
                },
            ),
            act,
            exp,
            CREDS,
            probe="store",
            server_check=max_lines("AUTHINFO USER", 2),
        )
    yield Scenario(
        "auth/481-after-480",
        script(
            step("GROUP g", b"480 login\r\n"),
            step("AUTHINFO USER u", b"381 pass\r\n"),
            step("AUTHINFO PASS p", b"481 bad\r\n"),
        ),
        lambda c: c.group("g"),
        Err(nntp.NNTPReplyError, state="sync", match="481"),
        {"reader": False, **CREDS},
    )
    yield Scenario(
        "auth/502-on-user",
        script(step("AUTHINFO USER u", b"502 no\r\n")),
        lambda c: c.login("u", "p"),
        Err(nntp.NNTPPermanentError, state="sync"),
        RAW,
    )
    yield Scenario(
        "auth/482-on-pass",
        script(
            step("AUTHINFO USER u", b"381 pass\r\n"),
            step("AUTHINFO PASS p", b"482 seq\r\n"),
        ),
        lambda c: c.login("u", "p"),
        Err(nntp.NNTPTemporaryError, state="sync"),
        RAW,
    )
    yield Scenario(
        "auth/381-no-password",
        script(step("AUTHINFO USER u", b"381 pass\r\n")),
        lambda c: c.login("u", ""),
        Err(nntp.NNTPReplyError, state="sync"),
        RAW,
        server_check=no_lines("AUTHINFO PASS"),
    )
    yield Scenario(
        "auth/480-on-authinfo",
        script(
            step("GROUP g", b"480 login\r\n"), step("AUTHINFO USER u", b"480 what\r\n")
        ),
        lambda c: c.group("g"),
        Err(nntp.NNTPTemporaryError, state="sync"),
        {"reader": False, **CREDS},
        server_check=max_lines("AUTHINFO USER", 1),
    )
    yield Scenario(
        "auth/480-mode-reader-login",
        script(
            step("MODE READER", b"480 login\r\n"),
            step("AUTHINFO USER u", b"381 pass\r\n"),
            step("AUTHINFO PASS p", b"281 ok\r\n"),
        ),
        None,
        Ok("connected"),
        {"reader": True, **CREDS},
        server_check=all_of(max_lines("MODE READER", 1), no_mismatch),
    )


def raw_subject(c: nntp.NNTPClient) -> object:
    rows = list(c.xover(1))
    return rows[0][1]["Subject"].encode("utf-8", "surrogateescape")


def malformed_scenarios() -> Iterator[Scenario]:
    """Malformed greetings, status lines and data."""
    greetings: list[tuple[str, bytes, type[BaseException]]] = [
        ("400", b"400 too busy\r\n", nntp.NNTPTemporaryError),
        ("502", b"502 go away\r\n", nntp.NNTPPermanentError),
        ("garbage", b"hello there\r\n", nntp.NNTPProtocolError),
    ]
    for name, greeting, exc in greetings:
        yield Scenario(
            f"malformed/greeting-{name}",
            script(greeting=greeting, probe=False),
            None,
            Err(exc),
            RAW,
            probe=None,
        )
    # RFC 3977 requires CRLF; a bounded failure is enough
    yield Scenario(
        "malformed/greeting-bare-lf",
        script(greeting=b"200 hi\n", probe=False),
        None,
        Err(*CLEAN_ERRORS),
        {"reader": False, "timeout": 1},
        probe=None,
        max_s=5,
    )
    yield Scenario(
        "malformed/status-code-600",
        script(step("BODY 1", b"600 what\r\n")),
        lambda c: c.body(1),
        Err(nntp.NNTPProtocolError, state="broken"),
        RAW,
    )
    yield Scenario(
        "malformed/status-code-only",
        script(step("BODY 1", ml(b"222", [b"x"]))),
        lambda c: c.body(1),
        Ok(b"x\r\n"),
        RAW,
    )
    yield Scenario(
        "malformed/listgroup-invalid-211",
        script(step("LISTGROUP g", ml(b"211 x y z g", [b"1", b"2"]))),
        lambda c: list(c.listgroup("g")),
        Ok([1, 2]),  # The 211 line isn't used, only the article numbers
        RAW,
    )
    yield Scenario(
        "malformed/group-negative-numbers",
        script(step("GROUP g", b"211 -5 -3 -9 g\r\n")),
        lambda c: c.group("g"),
        Err(nntp.NNTPDataError, state="sync"),
        RAW,
        known="group() accepts negative numbers in the 211 reply",
    )
    yield Scenario(
        "malformed/stat-msgid-without-at",
        script(step("STAT <abc>", b"223 0 <abc>\r\n")),
        lambda c: c.stat("<abc>"),
        Ok((0, "<abc>")),
        RAW,
    )
    yield Scenario(
        "malformed/over-nonnumeric-row",
        script(
            step("LIST OVERVIEW.FMT", OVERFMT),
            step("XOVER 1-2", ml(b"224 ov", [b"abc\tx", *OVER_ROWS])),
        ),
        lambda c: list(c.xover((1, 2))),
        Err(nntp.NNTPDataError),
        RAW,
    )
    yield Scenario(
        "malformed/non-utf8-subject",
        script(
            step("LIST OVERVIEW.FMT", OVERFMT),
            step(
                "XOVER 1",
                ml(
                    b"224 ov", [b"1\tsubj \xff\xfe\ta@b\td\t<1@x>\t\t1\t1\tXref: h g:1"]
                ),
            ),
        ),
        raw_subject,
        Ok(b"subj \xff\xfe"),
        RAW,
    )
    yield Scenario(
        "malformed/nul-in-body",
        script(step("BODY 1", ml(b"222 1 <a@b>", [b"a\x00b"]))),
        lambda c: c.body(1),
        Ok(b"a\x00b\r\n"),
        RAW,
    )
    yield Scenario(
        "malformed/article-bad-header-line",
        script(
            step(
                "ARTICLE 1",
                ml(b"220 1 <a@b>", [b"Subject: x", b"no colon here", b"", b"body"]),
            )
        ),
        lambda c: c.article(1),
        Err(nntp.NNTPDataError),
        RAW,
    )
    yield Scenario(
        "malformed/article-headers-only",
        script(step("ARTICLE 1", ml(b"220 1 <a@b>", [b"Subject: x"]))),
        lambda c: c.article(1)[2],
        Ok(b""),
        RAW,
    )
    yield Scenario(
        "malformed/terminator-bare-lf",
        script(step("BODY 1", b"222 1 <a@b>\r\nx\r\n.\n"), probe=False),
        lambda c: c.body(1),
        Err(*CLEAN_ERRORS, state="broken"),
        {"reader": False, "timeout": 1},
        probe=None,
        max_s=5,
    )


def abandon(c: nntp.NNTPClient) -> object:
    it = c.list_active()
    next(it)
    return c.date()


def stale_iterator(c: nntp.NNTPClient) -> object:
    it = c.list_active()
    next(it)  # abandoned part way; the next command is refused
    c.reconnect()  # as documented
    first = c.group("alt.test")
    del it  # the old generator is finalized only now
    return (first, attempt(lambda: c.group("alt.test")))


def rebind(c: nntp.NNTPClient) -> object:
    rows = c.list_active()
    for _ in rows:
        break  # keeps rows alive
    c.reconnect()
    rows = c.list_active()  # rebinding finalizes the old generator
    return attempt(lambda: sorted(g.name for g in rows))


def header_error(c: nntp.NNTPClient) -> object:
    with contextlib.suppress(nntp.NNTPDataError):
        c.article("<9@test>")
    c.reconnect()
    first = c.group("alt.test")
    gc.collect()
    return (first, attempt(lambda: c.group("alt.test")))


def resume_old(c: nntp.NNTPClient) -> object:
    old = c.list_active()
    next(old)
    c.reconnect()
    new = c.list_active()
    got_new = next(new)
    got_old = attempt(lambda: next(old))
    return (got_new.name, getattr(got_old, "name", got_old))


def session_scenarios() -> Iterator[Scenario]:
    """Response iterators and reconnect()."""
    yield Scenario(
        "session/abandoned-iterator",
        store_cfg(),
        abandon,
        Err(nntp.NNTPSyncError),
        probe=None,
    )
    yield Scenario(
        "session/abandoned-iterator-finalized-after-reconnect",
        store_cfg(),
        stale_iterator,
        Ok(((3, 1, 3, "alt.test"), (3, 1, 3, "alt.test"))),
        probe="store",
    )
    yield Scenario(
        "session/iterator-variable-reused-after-reconnect",
        store_cfg(),
        rebind,
        Ok(["alt.empty", "alt.test"]),
        probe="store",
    )
    bad = store_cfg()
    bad["store"]["articles"].append(
        {
            "group": "alt.test",
            "number": 9,
            "msgid": "<9@test>",
            "headers": ["Subject: x", "no colon here"],
            "body": ["b"],
        }
    )
    yield Scenario(
        "session/header-parse-error-then-reconnect",
        bad,
        header_error,
        Ok(pred=lambda v: v[0] == v[1] == (4, 1, 9, "alt.test"), desc="both succeed"),
        probe="store",
    )
    # Misuse, but it shows that iterators are not bound to their connection
    yield Scenario(
        "session/resume-abandoned-iterator-after-reconnect",
        store_cfg(),
        resume_old,
        Clean(
            pred=lambda v: not (isinstance(v[1], str) and v[1].startswith("alt.")),
            desc="the old iterator must not yield lines of the new connection",
        ),
        probe=None,
        known="a response iterator resumed after reconnect() reads the new "
        "connection's reply",
    )


@cache
def longline_scenarios() -> Iterator[Scenario]:
    """A response line at and over max_line_length (64 KiB with CRLF)."""
    limit = nntp.NNTPClient.max_line_length
    too_long = Err(nntp.NNTPDataError, state="broken", match="Line longer than")

    def body(c: nntp.NNTPClient) -> object:
        return c.body("<1@test>", decode=False)

    yield Scenario(
        "longline/body-at-limit",
        store_cfg(faults={"long_line": {"BODY": limit - 2}}),
        body,
        Ok(b"x" * (limit - 2) + b"\r\n" + STORE_BODY_BYTES),
        probe="store",
    )
    yield Scenario(
        "longline/body-over-limit",
        store_cfg(faults={"long_line": {"BODY": limit - 1}}),
        body,
        too_long,
        probe="store",
    )
    yield Scenario(
        "longline/over-over-limit",
        store_cfg(faults={"long_line": {"OVER": limit - 1}}),
        in_group(lambda c: list(c.over((1, 3)))),
        too_long,
        probe="store",
    )
    yield Scenario(
        "longline/compress-body-over-limit",
        store_cfg(store={"compress": True}, faults={"long_line": {"BODY": limit - 1}}),
        compressed(body),
        too_long,
        probe="store",
    )


def scenarios() -> list[Scenario]:
    """All scenarios; names are unique."""
    families: list[Callable[[], Iterator[Scenario]]] = [
        split_scenarios,
        code_scenarios,
        leftover_scenarios,
        drop_scenarios,
        timeout_scenarios,
        tls_scenarios,
        compress_store_scenarios,
        compress_raw_scenarios,
        gzip_scenarios,
        xzver_scenarios,
        yenc_scenarios,
        post_scenarios,
        ihave_scenarios,
        auth_scenarios,
        malformed_scenarios,
        longline_scenarios,
        session_scenarios,
    ]
    return [sc for family in families for sc in family()]


# ---------------------------------------------------------------------------
# Client side (child process)


def marked_broken(c: nntp.NNTPClient) -> bool:
    try:
        c.date()
    except nntp.NNTPSyncError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def names(types: tuple[type[BaseException], ...]) -> str:
    return "/".join(t.__name__ for t in types)


def probe_state(c: nntp.NNTPClient, sc: Scenario) -> tuple[str, str]:
    """Send DATE: is the connection in sync, marked broken, or neither?"""
    try:
        d = c.date()
    except nntp.NNTPSyncError as e:
        return "broken", str(e)
    except CLEAN_ERRORS as e:
        # A clean failure is fine if it marked the connection broken
        state = "late-broken" if marked_broken(c) else "desync"
        return state, f"probe raised {type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        return "desync", f"probe raised {type(e).__name__}: {e}"
    got = d.timetuple()[:6]
    good = got == PROBE_TUPLE if sc.probe == "raw" else got[0] >= 2025
    return ("sync", "") if good else ("desync", f"probe got stale/wrong DATE {got}")


def judge_error(sc: Scenario, e: BaseException, state: str) -> str:
    exp = sc.expect
    what = f"{type(e).__name__}: {e}"
    if not isinstance(e, CLEAN_ERRORS):
        return f"unexpected exception {what}"
    if not isinstance(exp, Err):
        return f"expected a result, got {what}" if isinstance(exp, Ok) else ""
    problems = [
        (not isinstance(e, exp.types), f"expected {names(exp.types)}, got {what}"),
        (
            bool(exp.match)
            and not re.search(exp.match, f"{getattr(e, 'code', '')} {e}"),
            f"{what} does not match {exp.match!r}",
        ),
        (exp.pred is not None and not exp.pred(e), f"{what} fails the check"),
        (
            exp.state == "sync" and sc.probe is not None and state != "sync",
            f"expected the connection to stay in sync, it is {state}",
        ),
        (
            exp.state == "broken" and sc.probe is not None and state != "broken",
            f"expected the connection marked broken, it is {state}",
        ),
    ]
    return next((why for bad, why in problems if bad), "")


def judge_value(sc: Scenario, value: object, state: str) -> str:
    exp = sc.expect
    if isinstance(exp, Err):
        return f"expected {names(exp.types)}, got result {value!r:.200}"
    if isinstance(exp, Clean):
        return (
            "" if exp.pred is None or exp.pred(value) else f"{value!r:.300}: {exp.desc}"
        )
    good = exp.pred(value) if exp.pred else value == exp.value
    if not good:
        return (
            f"wrong result {value!r:.300}, expected {exp.desc or repr(exp.value):.200}"
        )
    if sc.probe is not None and state != "sync":
        return f"result ok but the connection is {state}"
    return ""


def client_kwargs(sc: Scenario, ca: str) -> JSON:
    kw: JSON = {"timeout": 5, "reader": True}
    kw.update({k: v for k, v in sc.connect.items() if k not in ("ca", "ssl_mode")})
    if sc.connect.get("ca"):
        kw["ssl_context"] = ssl.create_default_context(cafile=ca)
    if sc.connect.get("ssl_mode") == "STARTTLS":
        kw["ssl_mode"] = nntp.SSLMode.STARTTLS
    return kw


def run_client(sc: Scenario, host: str, port: int, ca: str) -> JSON:
    """Run the client side of sc; the result says why it failed, if it did."""
    # A runaway client fails the scenario instead of exhausting the machine.
    # Not on free-threaded builds: their allocator reserves much address space.
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    t0 = time.monotonic()
    c = None
    value: object = None
    error: BaseException | None = None
    try:
        c = nntp.NNTPClient(host, port, **client_kwargs(sc, ca))
        value = sc.action(c) if sc.action else "connected"
    except Exception as e:  # noqa: BLE001
        error = e
    elapsed = time.monotonic() - t0
    state, detail = "n/a", ""
    if c is not None and sc.probe:
        state, detail = probe_state(c, sc)
    if c is not None:
        with contextlib.suppress(Exception):
            c.close()
    if sc.max_s and elapsed > sc.max_s:
        reason = f"took {elapsed:.1f}s (max {sc.max_s}s)"
    elif state == "desync":
        reason = f"connection neither in sync nor marked broken: {detail}"
    elif error is not None:
        reason = judge_error(sc, error, state)
    else:
        reason = judge_value(sc, value, state)
    return {
        "reason": reason,
        "elapsed": round(elapsed, 3),
        "state": state,
        "result": f"{type(error).__name__}: {error}" if error else repr(value)[:400],
    }


# ---------------------------------------------------------------------------
# Server side (parent process)


# The scenarios use short keys; nntp-fakeserver takes the kit's Go field names
OPTION_KEYS = {
    "greeting": "Greeting",
    "no_greeting": "NoGreeting",
    "raw_stall": "RawStall",
    "tls": "TLS",
    "deadline_ms": "Deadline",
}
FAULT_KEYS = {
    "delay_ms": "Delay",
    "close_after": "CloseAfter",
    "override": "Override",
    "override_line": "OverrideLine",
    "drop_in_multiline": "DropInMultiline",
    "drip_ms": "Drip",
    "endless": "Endless",
    "line_delay_ms": "LineDelay",
    "override_from": "OverrideFrom",
    "override_times": "OverrideTimes",
    "long_line": "LongLine",
}
STORE_KEYS = {
    "user": "User",
    "pass": "Pass",
    "reader_only": "ReaderOnly",
    "compress": "Compress",
    "compress_output_only": "CompressOutputOnly",
    "listgroup_range": "ListgroupRange",
    "no_over": "NoOver",
    "no_xover": "NoXover",
}
PEER_KEYS = {
    "user": "User",
    "pass": "Pass",
    "have": "Have",
    "defer436": "Defer436Once",
    "reject": "Reject",
    "compress": "Compress",
}
STEP_KEYS = {
    "expect": "Expect",
    "read_dot": "ReadDot",
    "dot_max": "DotMax",
    "sleep_ms": "Sleep",
    "then": "Then",
}
CHUNK_KEYS = {
    "b64": "B64",
    "gap_ms": "Gap",
    "split": "Split",
    "split_gap_ms": "SplitGap",
}


def renamed(d: JSON, keys: dict[str, str], what: str) -> JSON:
    """d with its keys renamed; an unknown key is an error, not dropped."""
    if unknown := set(d) - set(keys):
        msg = f"unknown {what} keys: {sorted(unknown)}"
        raise ValueError(msg)
    return {keys[k]: v for k, v in d.items()}


def kit_config(server: JSON) -> JSON:
    """A scenario's server config in nntp-fakeserver's schema."""
    s = dict(server)
    mode = s.pop("mode")
    out: JSON = {"Mode": mode}
    store, peer = s.pop("store", None), s.pop("peer", None)
    raw, faults = s.pop("raw", None), s.pop("faults", None)
    options = renamed(s, OPTION_KEYS, "server")
    if faults:
        options["Faults"] = renamed(faults, FAULT_KEYS, "fault")
    if store is not None:
        store = dict(store)
        groups = store.pop("groups", [])
        articles = store.pop("articles", [])
        options["StoreOpts"] = renamed(store, STORE_KEYS, "store")
        out["Store"] = {
            "Groups": [{"Name": n, "Description": d} for n, d in groups],
            "Articles": [
                {
                    "Group": a["group"],
                    "Number": a["number"],
                    "MessageID": a["msgid"],
                    "Headers": a["headers"],
                    "Body": a["body"],
                }
                for a in articles
            ],
        }
    if peer is not None:
        out["PeerOptions"] = renamed(peer, PEER_KEYS, "peer")
    if raw is not None:
        out["Raw"] = [
            {
                **renamed(
                    {k: v for k, v in st.items() if k != "send"}, STEP_KEYS, "step"
                ),
                "Send": [renamed(c, CHUNK_KEYS, "chunk") for c in st["send"]],
            }
            for st in raw
        ]
    if options:
        out["Options"] = options
    return out


def wire_summary(server: JSON) -> list[str]:
    """Readable server bytes of a script, for failure messages."""
    out = []
    for st in server.get("raw", []):
        data = b"".join(base64.b64decode(ch["b64"]) for ch in st["send"])
        then = f" then {st['then']}" if st["then"] else ""
        out.append(f"C: {st['expect']!s:<24} S: {data!r:.300}{then}")
    return out or [json.dumps({k: v for k, v in server.items() if k != "store"})]


def run(sc: Scenario, server: Path) -> JSON:
    """Run sc against the server binary; result["reason"] is "" if it passed."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp, "scenario.json")
        cfg.write_text(json.dumps(kit_config(sc.server)))
        with subprocess.Popen(  # noqa: S603
            [server, cfg], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        ) as srv:
            assert srv.stdin is not None
            assert srv.stdout is not None
            info = json.loads(srv.stdout.readline())
            cmd = [sys.executable, "-I", __file__, sc.name, info["host"]]
            cmd += [str(info["port"]), info["ca"]]
            try:
                child = subprocess.run(  # noqa: S603
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=sc.deadline,
                    check=False,
                )
                out = [ln for ln in child.stdout.splitlines() if ln.startswith("{")]
                res: JSON = (
                    json.loads(out[-1])
                    if out
                    else {"reason": f"client died: {child.stderr.strip()[-600:]}"}
                )
            except subprocess.TimeoutExpired:
                res = {"reason": f"hang: no result within {sc.deadline}s"}
            srv.stdin.close()
            summary = json.loads(srv.stdout.readline() or "{}")
            srv.wait(10)
    if not res["reason"] and sc.server_check and summary:
        summary["articles"] = summary.get("articles") or []
        if why := sc.server_check(summary):
            res["reason"] = f"server side: {why}"
    res["wire"] = wire_summary(sc.server)
    res["client_lines"] = summary.get("lines", [])[:20]
    return res


if __name__ == "__main__":
    name, host, port, ca = sys.argv[1:]
    sc = next(s for s in scenarios() if s.name == name)
    print(json.dumps(run_client(sc, host, int(port), ca)))
