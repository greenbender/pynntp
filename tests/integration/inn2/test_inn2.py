"""Integration tests against a real server, by default INN on localhost.

They run against the greenbender/inn image and against a stock INN set up
with setup.sh, and with the variables below against other servers too. The
groups may already hold articles, so each test posts its own and finds it by
Message-ID, and tests of optional features skip when CAPABILITIES doesn't
list them. Checks of INN's own setup (its default groups, the "junk" group)
run only against INN. The tests post permanent articles.

Environment:
    PYNNTP_HOST: The server (default localhost).
    PYNNTP_PORT: Its plain-text and STARTTLS port (default 119).
    PYNNTP_TLS_PORT: Its implicit TLS port (default 563); 0 skips the
        implicit TLS tests.
    PYNNTP_USER, PYNNTP_PASS: Credentials for a server that wants a login;
        the client logs in when the server answers 480, and the COMPRESS
        tests log in before compressing.
    PYNNTP_GROUPS: Comma-separated groups the tests may post to, the last
        one used by most tests (default local.general,local.test). Each must
        be listed by LIST ACTIVE with status "y", and posted articles must
        show in its Xref header.
    PYNNTP_EMPTY_GROUP: An empty group, for the empty-group test (default
        junk on INN; the test skips on other servers without it).
    PYNNTP_TLS_CAFILE: Certificate to verify the TLS server against. Without
        it, TLS tests don't check the certificate.
    PYNNTP_POST_WAIT: Seconds to wait for a posted article to be listed in
        its group (default 10).
"""

import email.utils
import functools
import os
import re
import ssl
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import nntp
from nntp.types import SSLMode

DEFAULT_NEWGROUPS = {
    ("control", "Various control messages (no posting)"),
    ("control.cancel", "Cancel messages (no posting)"),
    ("control.checkgroups", "Hierarchy check control messages (no posting)"),
    ("control.newgroup", "Newsgroup creation control messages (no posting)"),
    ("control.rmgroup", "Newsgroup removal control messages (no posting)"),
    ("junk", "Unfiled articles (no posting)"),
    ("local.general", "Local general group"),
    ("local.test", "Local test group"),
}


def env(name: str, default: str) -> str:
    """The environment variable, stripped; unset or empty means default."""
    return os.environ.get(name, "").strip() or default


HOST = env("PYNNTP_HOST", "localhost")
PORT = int(env("PYNNTP_PORT", "119"))
TLS_PORT = int(env("PYNNTP_TLS_PORT", "563"))
USER = os.environ.get("PYNNTP_USER", "")
PASSWORD = os.environ.get("PYNNTP_PASS", "")
GROUPS = [
    group.strip()
    for group in env("PYNNTP_GROUPS", "local.general,local.test").split(",")
    if group.strip()
]
if not GROUPS:
    raise ValueError("PYNNTP_GROUPS names no group")
GROUP = GROUPS[-1]
EMPTY_GROUP = env("PYNNTP_EMPTY_GROUP", "")
TLS_CAFILE = os.environ.get("PYNNTP_TLS_CAFILE")
POST_WAIT = float(env("PYNNTP_POST_WAIT", "10"))


def client(**kwargs: Any) -> nntp.NNTPClient:  # noqa: ANN401
    """A client for the test server, with its port and credentials."""
    implicit = kwargs.get("use_ssl") and kwargs.get("ssl_mode") != SSLMode.STARTTLS
    if implicit and not TLS_PORT:
        pytest.skip("PYNNTP_TLS_PORT=0: no implicit TLS")
    port = TLS_PORT if implicit else PORT
    return nntp.NNTPClient(HOST, port, username=USER, password=PASSWORD, **kwargs)


def check_newsgroups(nntp_client: nntp.NNTPClient) -> None:
    """Check that LIST NEWSGROUPS has INN's default groups, or the test groups.

    The server may have more. INN 2.8 ends the descriptions with a period.
    """
    if is_inn():
        newsgroups = {
            (name, description.removesuffix("."))
            for name, description in nntp_client.list_newsgroups()
        }
        assert newsgroups >= DEFAULT_NEWGROUPS
    else:
        # LIST NEWSGROUPS may leave out groups without a description
        names = {
            group.name for name in GROUPS for group in nntp_client.list_active(name)
        }
        assert names >= set(GROUPS)


@functools.cache
def capabilities() -> tuple[str, ...]:
    """The server's CAPABILITIES in reader mode, or () if it has none."""
    with client() as nntp_client:
        try:
            return tuple(nntp_client.capabilities())
        except nntp.NNTPPermanentError:  # 5xx: not implemented (RFC 3977 5.2)
            return ()


def require(capability: str) -> None:
    """Skip the test unless CAPABILITIES lists capability.

    The first word of a line must match and the other words must be on the
    line too, so "COMPRESS DEFLATE" and "LIST ACTIVE.TIMES" both work.
    """
    name, *keywords = capability.split()
    for line in capabilities():
        words = line.split()
        if words[:1] == [name] and all(keyword in words for keyword in keywords):
            return
    pytest.skip(f"The server does not offer {capability}")


def inn_version() -> tuple[int, ...]:
    """INN's version from CAPABILITIES, or () for other servers."""
    for line in capabilities():
        if match := re.match(r"IMPLEMENTATION INN (\d+)\.(\d+)\.(\d+)", line):
            return tuple(int(part) for part in match.groups())
    return ()


def is_inn() -> bool:
    """Whether the server is INN, whose default setup some tests check."""
    return any(line.startswith("IMPLEMENTATION INN ") for line in capabilities())


def require_inn() -> None:
    if not is_inn():
        pytest.skip("Checks INN's default setup")


def empty_group() -> str:
    """An empty group: PYNNTP_EMPTY_GROUP, or INN's junk."""
    if EMPTY_GROUP:
        return EMPTY_GROUP
    require_inn()
    return "junk"


def article_headers(subject: str, newsgroup: str = GROUP) -> dict[str, str]:
    """Headers for a new article, with a unique Message-ID."""
    return {
        "From": "GitHub Actions <actions@github.com>",
        "Newsgroups": newsgroup,
        "Subject": subject,
        "Message-ID": f"<pynntp.{uuid.uuid4().hex}@example.com>",
    }


def ihave_headers(subject: str) -> dict[str, str]:
    """Headers for a new article to offer with IHAVE, which also needs these."""
    return {
        **article_headers(subject),
        "Path": "example.com!not-for-mail",
        "Date": email.utils.formatdate(),
    }


def number_in_group(
    nntp_client: nntp.NNTPClient, message_id: str, newsgroup: str = GROUP
) -> int:
    """Return the number of an article in newsgroup, which is then selected.

    The number comes from the Xref header, which is optional (RFC 5536), so
    the test skips without one. Waits up to PYNNTP_POST_WAIT seconds for the
    article to be listed in the group.
    """
    deadline = time.monotonic() + POST_WAIT
    while True:
        try:
            headers = nntp_client.head(message_id)
            if "Xref" not in headers:
                pytest.skip("The server sends no Xref header")
            numbers = {
                group: number
                for group, _, number in (
                    ref.partition(":") for ref in headers["Xref"].split()
                )
            }
            if newsgroup in numbers:
                number = int(numbers[newsgroup])
                _, first, last, _ = nntp_client.group(newsgroup)
                if first <= number <= last:
                    return number
        except nntp.NNTPTemporaryError as e:
            if e.code != 430:  # No such article (yet)
                raise
        if time.monotonic() > deadline:
            raise AssertionError(f"{message_id} is not listed in {newsgroup}")
        time.sleep(0.2)


def post(
    nntp_client: nntp.NNTPClient,
    subject: str,
    newsgroup: str = GROUP,
    body: str = "",
) -> tuple[int, str]:
    """Post an article and return its number in newsgroup and its Message-ID."""
    headers = article_headers(subject, newsgroup)
    message_id = headers["Message-ID"]
    # INN replies "240 Article received <id>"; the message-id is optional.
    assert nntp_client.post(headers, body or f"{subject}\n") in (message_id, True)
    return number_in_group(nntp_client, message_id, newsgroup), message_id


def tls_context() -> ssl.SSLContext:
    """TLS settings for the test server's self-signed certificate."""
    if TLS_CAFILE:
        return ssl.create_default_context(cafile=TLS_CAFILE)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def compress(nntp_client: nntp.NNTPClient) -> None:
    """COMPRESS DEFLATE, logging in first: a login isn't allowed after it."""
    if USER:
        nntp_client.login()
    nntp_client.compress()


def test_nntp_client() -> None:
    nntp_client = client()
    check_newsgroups(nntp_client)


def test_context_manager() -> None:
    """The client works as a context manager and sends QUIT on exit."""
    with client() as nntp_client:
        check_newsgroups(nntp_client)


def test_context_manager_on_close() -> None:
    """close() inside the with block is safe; __exit__ does nothing more."""
    with client() as nntp_client:
        check_newsgroups(nntp_client)
        nntp_client.close()


def test_context_manager_on_quit() -> None:
    """quit() inside the with block is safe; __exit__ does nothing more."""
    with client() as nntp_client:
        check_newsgroups(nntp_client)
        nntp_client.quit()


def test_nntp_client_without_ssl() -> None:
    with client(use_ssl=False) as nntp_client:
        check_newsgroups(nntp_client)


def test_tls_verifies_certificate_by_default() -> None:
    """The default trust store rejects the server's certificate.

    A publicly trusted certificate passes it, so the test skips then. This
    holds with or without PYNNTP_TLS_CAFILE, which the other TLS tests use.
    """
    try:
        nntp_client = client(use_ssl=True)
    except ssl.SSLCertVerificationError:
        return
    nntp_client.close()
    pytest.skip("The default trust store accepts the server's certificate")


def test_nntp_client_with_ssl() -> None:
    with client(use_ssl=True, ssl_context=tls_context()) as nntp_client:
        check_newsgroups(nntp_client)


def test_nntp_client_with_ssl_starttls() -> None:
    require("STARTTLS")
    with client(
        use_ssl=True, ssl_mode=SSLMode.STARTTLS, ssl_context=tls_context()
    ) as nntp_client:
        check_newsgroups(nntp_client)


@pytest.mark.parametrize("newsgroup", GROUPS)
def test_post(newsgroup: str) -> None:
    with client() as nntp_client:
        _, _, last, _ = nntp_client.group(newsgroup)
        number, message_id = post(nntp_client, f"Test post to {newsgroup}", newsgroup)
        assert number > last
        assert nntp_client.stat(message_id) == (0, message_id)


@pytest.mark.parametrize("newsgroup", [*GROUPS, "junk"])
def test_list_active(newsgroup: str) -> None:
    if newsgroup == "junk":
        require_inn()
    with client() as nntp_client:
        _, first, last, _ = nntp_client.group(newsgroup)
        [(name, low, high, status)] = nntp_client.list_active(newsgroup)
        assert (name, low, high) == (newsgroup, first, last)
        assert status == ("n" if newsgroup == "junk" else "y")


@pytest.mark.parametrize("newsgroup", GROUPS)
def test_article(newsgroup: str) -> None:
    body = f"This is a test post to {newsgroup}\n"
    with client() as nntp_client:
        number, message_id = post(nntp_client, "Article test", newsgroup, body)
        total, first, last, group = nntp_client.group(newsgroup)
        assert total >= 1
        assert first <= number <= last
        assert group == newsgroup
        nntp_client.stat(number)
        article_number, headers, article_body = nntp_client.article()
        assert article_number == number
        assert headers["Message-ID"] == message_id
        assert headers["Newsgroups"] == newsgroup
        assert headers["Subject"] == "Article test"
        assert article_body == body.replace("\n", "\r\n").encode()


def test_post_to_junk_is_refused() -> None:
    require_inn()
    headers = {"Subject": "x", "From": "a@example.com", "Newsgroups": "junk"}
    with client() as nntp_client:
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.post(headers=headers, body="x")
        assert info.value.code == 441


def test_article_in_empty_group() -> None:
    group = empty_group()
    with client() as nntp_client:
        total, low, high, name = nntp_client.group(group)
        # RFC 3977 6.1.1.2: an empty group reports high = low - 1 (INN: 1 0),
        # all zeros, or a high water mark below the low one
        assert (total, name) == (0, group)
        assert high < low or (low, high) == (0, 0)
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.article()
        assert info.value.code == 420


SINCE = datetime(2000, 1, 1, tzinfo=UTC)


def test_newgroups() -> None:
    """NEWGROUPS lists the groups that LIST ACTIVE.TIMES says are newer."""
    require("LIST ACTIVE.TIMES")
    with client() as nntp_client:
        active = {group.name for group in nntp_client.list_active()}
        created = {
            name
            for name, timestamp, _ in nntp_client.list_active_times()
            if timestamp >= SINCE and name in active
        }
        assert created <= {group.name for group in nntp_client.newgroups(SINCE)}
        tomorrow = nntp_client.date() + timedelta(days=1)  # By the server's clock
        assert list(nntp_client.newgroups(tomorrow)) == []


def test_newnews() -> None:
    require("NEWNEWS")
    with client() as nntp_client:
        since = nntp_client.date() - timedelta(minutes=1)
        _, message_id = post(nntp_client, "NEWNEWS test")
        msgids = list(nntp_client.newnews(GROUP, since))
        assert message_id in msgids
        assert all(m.startswith("<") and m.endswith(">") for m in msgids)
        tomorrow = nntp_client.date() + timedelta(days=1)
        assert list(nntp_client.newnews(GROUP, tomorrow)) == []


def test_xover_full_field() -> None:
    require("OVER")
    with client() as nntp_client:
        number, message_id = post(nntp_client, "Overview test")
        [(article_number, overview)] = nntp_client.xover((number, number))
        assert article_number == number
        assert overview["subject"] == "Overview test"
        assert overview["message-id"] == message_id
        if "xref" not in overview:
            pytest.skip("The overview has no Xref field (RFC 3977 8.3.2)")
        # Xref is a "full" field: the "Xref: " prefix is stripped.
        assert overview["xref"].endswith(f" {GROUP}:{number}")
        assert not overview["xref"].lower().startswith("xref")


def test_xhdr() -> None:
    """XHDR (RFC 2980) isn't gated by CAPABILITIES: skip if the server refuses it."""
    subject = "XHDR test"
    with client() as nntp_client:
        number, message_id = post(nntp_client, subject)
        span = (number, number)
        try:
            by_number = list(nntp_client.xhdr("Subject", span))
            # By message-id, the article number is 0
            by_message_id = list(nntp_client.xhdr("Subject", message_id))
        except nntp.NNTPPermanentError as e:
            pytest.skip(f"The server refuses XHDR: {e.code}")
        assert by_number == [(number, subject)]
        assert by_message_id == [(0, subject)]


def test_hdr_over_stat_listgroup() -> None:
    require("HDR")
    require("OVER")
    subject = "HDR test"
    with client() as nntp_client:
        number, message_id = post(nntp_client, subject)
        span = (number, number)
        assert list(nntp_client.hdr("Subject", span)) == [(number, subject)]
        # By message-id, the article number is 0
        assert list(nntp_client.hdr("Subject", message_id)) == [(0, subject)]
        [(article_number, overview)] = nntp_client.over(span)
        assert (article_number, overview["message-id"]) == (number, message_id)
        assert nntp_client.stat(number) == (number, message_id)
        assert nntp_client.stat(message_id) == (0, message_id)
        assert number in list(nntp_client.listgroup(GROUP))
        assert list(nntp_client.listgroup(GROUP, span)) == [number]


def test_ihave() -> None:
    require("IHAVE")
    headers = ihave_headers("IHAVE test")
    message_id = headers["Message-ID"]
    body = "IHAVE body\n.dot\n.\n"
    with client() as nntp_client:
        nntp_client.ihave(message_id, headers, body)
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.ihave(message_id, headers, body)
        assert info.value.code == 435  # Duplicate: not wanted
        assert nntp_client.stat(message_id) == (0, message_id)
        assert nntp_client.body(message_id) == b"IHAVE body\r\n.dot\r\n.\r\n"


@pytest.mark.parametrize("ssl_mode", [None, SSLMode.IMPLICIT, SSLMode.STARTTLS])
def test_compress(ssl_mode: SSLMode | None) -> None:
    require("COMPRESS DEFLATE")
    if ssl_mode == SSLMode.STARTTLS:
        require("STARTTLS")
    with client(
        use_ssl=ssl_mode is not None,
        ssl_mode=ssl_mode or SSLMode.IMPLICIT,
        ssl_context=tls_context(),
    ) as nntp_client:
        compress(nntp_client)
        assert "COMPRESS DEFLATE" not in list(nntp_client.capabilities())
        check_newsgroups(nntp_client)
        assert nntp_client.date().year >= 2024
        with pytest.raises(nntp.NNTPError, match="not allowed after COMPRESS"):
            nntp_client.mode_reader()


@pytest.mark.parametrize("verb", ["POST", "IHAVE"])
def test_compressed_article(verb: str) -> None:
    """An article larger than one write is sent compressed and arrives intact."""
    require("COMPRESS DEFLATE")
    require(verb)
    version = inn_version()
    if version and version < (2, 7, 2):
        pytest.skip("INN before 2.7.2 can hang on compressed POST and IHAVE")
    headers = ihave_headers(f"Compressed {verb}")
    message_id = headers["Message-ID"]
    body = ".first\n.\n" + ("x" * 200 + "\n") * 400 + "last\n"
    with client() as nntp_client:
        compress(nntp_client)
        if verb == "POST":
            assert nntp_client.post(headers, body) in (message_id, True)
        else:
            nntp_client.ihave(message_id, headers, body)
        number = number_in_group(nntp_client, message_id)
        assert nntp_client.body(number) == body.replace("\n", "\r\n").encode()
        assert nntp_client.head(number)["Subject"] == f"Compressed {verb}"


def test_reconnect_after_abandoned_response() -> None:
    with client() as nntp_client:
        nntp_client.group(GROUP)
        groups = nntp_client.list_active()
        next(groups)
        with pytest.raises(nntp.NNTPSyncError):
            nntp_client.date()
        del groups  # Abandoned part way
        nntp_client.reconnect()
        # A new session, so no group is selected
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.stat()
        assert info.value.code == 412


def test_login_refused_keeps_connection() -> None:
    with client() as nntp_client:
        with pytest.raises(nntp.NNTPReplyError) as info:
            nntp_client.login("user", "secret")
        # 502: no logins here. INN with TLS set up wants TLS first (483).
        # A server with logins refuses the wrong one (481, RFC 4643).
        assert info.value.code in {481, 483, 502}
        assert nntp_client.date().year >= 2024


def test_xpat() -> None:
    require("XPAT")
    subject = f"XPAT test {uuid.uuid4().hex}"
    with client() as nntp_client:
        number, _ = post(nntp_client, subject)
        pattern = f"*{subject[-8:]}*"
        assert list(nntp_client.xpat("Subject", (number,), pattern)) == [
            (number, subject)
        ]


@pytest.mark.parametrize("scheme", ["nntp", "nntps", "nntp+starttls"])
def test_from_url(scheme: str) -> None:
    if scheme == "nntp+starttls":
        require("STARTTLS")
    port = PORT
    if scheme == "nntps":
        if not TLS_PORT:
            pytest.skip("PYNNTP_TLS_PORT=0: no implicit TLS")
        port = TLS_PORT
    url = f"{scheme}://{HOST}:{port}"
    with nntp.NNTPClient.from_url(
        url, username=USER, password=PASSWORD, ssl_context=tls_context()
    ) as nntp_client:
        check_newsgroups(nntp_client)


def test_from_url_with_options() -> None:
    url = f"nntp://{HOST}:{PORT}?timeout=5&reader=false"
    with nntp.NNTPClient.from_url(url, username=USER, password=PASSWORD) as nntp_client:
        assert nntp_client.socket.gettimeout() == 5
        assert nntp_client.date().year >= 2024
