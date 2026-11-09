import email.utils
import os
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

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


def test_nntp_client() -> None:
    nntp_client = nntp.NNTPClient("localhost")
    newsgroups = set(nntp_client.list_newsgroups())
    assert newsgroups == DEFAULT_NEWGROUPS


def test_context_manager() -> None:
    """The client works as a context manager and sends QUIT on exit."""
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


def test_context_manager_on_close() -> None:
    """close() inside the with block is safe; __exit__ does nothing more."""
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS
        nntp_client.close()


def test_context_manager_on_quit() -> None:
    """quit() inside the with block is safe; __exit__ does nothing more."""
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS
        nntp_client.quit()


def test_nntp_client_without_ssl() -> None:
    with nntp.NNTPClient("localhost", use_ssl=False) as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@mock.patch.dict(os.environ, {"PYNNTP_TLS_INSECURE": "1"})
def test_nntp_client_with_ssl() -> None:
    with nntp.NNTPClient("localhost", 563, use_ssl=True) as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@mock.patch.dict(os.environ, {"PYNNTP_TLS_INSECURE": "1"})
def test_nntp_client_with_ssl_starttls() -> None:
    with nntp.NNTPClient(
        "localhost", use_ssl=True, ssl_mode=SSLMode.STARTTLS
    ) as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@pytest.mark.parametrize("newsgroup", ["local.general", "local.test"])
def test_post(newsgroup: str) -> None:
    headers = {
        "Subject": f"Test post to {newsgroup}",
        "From": "GitHub Actions <actions@github.com>",
        "Newsgroups": newsgroup,
    }
    with nntp.NNTPClient("localhost") as nntp_client:
        assert (
            nntp_client.post(
                headers=headers, body=f"This is a test post to {newsgroup}"
            )
            is True
        )


@pytest.mark.parametrize("newsgroup", ["local.general", "local.test", "junk"])
def test_list_active(newsgroup: str) -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        articles = nntp_client.list_active(newsgroup)
        for name, low, high, status in articles:
            assert name == newsgroup
            # junk is empty (high < low); the others have the one test post.
            assert (low, high) == ((1, 0) if newsgroup == "junk" else (1, 1))
            assert status == ("n" if newsgroup == "junk" else "y")


@pytest.mark.parametrize("newsgroup", ["local.general", "local.test"])
def test_article(newsgroup: str) -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        total, first, last, group = nntp_client.group(newsgroup)
        assert (total, first, last) == (1, 1, 1)
        assert group == newsgroup
        article_number, headers, body = nntp_client.article()
        assert article_number == 1
        assert headers["Newsgroups"] == newsgroup
        assert headers["Subject"] == f"Test post to {newsgroup}"
        assert body == f"This is a test post to {newsgroup}\r\n".encode()


def test_post_to_junk_is_refused() -> None:
    headers = {"Subject": "x", "From": "a@example.com", "Newsgroups": "junk"}
    with nntp.NNTPClient("localhost") as nntp_client:
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.post(headers=headers, body="x")
        assert info.value.code == 441


def test_article_in_empty_group() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        assert nntp_client.group("junk") == (0, 1, 0, "junk")
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.article()
        assert info.value.code == 420


SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)


def test_newgroups() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        names = {group.name for group in nntp_client.newgroups(SINCE)}
        assert {"local.general", "local.test"} <= names
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        assert list(nntp_client.newgroups(tomorrow)) == []


def test_newnews() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        msgids = list(nntp_client.newnews("local.*", SINCE))
        assert msgids
        assert all(m.startswith("<") and m.endswith(">") for m in msgids)


def test_xover_full_field() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        nntp_client.group("local.test")
        [(number, overview)] = nntp_client.xover((1, 1))
        assert number == 1
        assert overview["subject"] == "Test post to local.test"
        # Xref is a "full" field: the "Xref: " prefix is stripped.
        assert overview["xref"].endswith(" local.test:1")
        assert not overview["xref"].lower().startswith("xref")


def test_hdr_over_stat_listgroup() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        _, first, last, _ = nntp_client.group("local.test")
        subject = "Test post to local.test"
        assert list(nntp_client.hdr("Subject", (first, last))) == [(1, subject)]
        assert list(nntp_client.xhdr("Subject", (first, last))) == [(1, subject)]
        [(number, overview)] = nntp_client.over((first, last))
        assert (number, overview["subject"]) == (1, subject)
        msgid = overview["message-id"]
        assert nntp_client.stat(1) == (1, msgid)
        assert nntp_client.stat(msgid)[1] == msgid
        assert list(nntp_client.listgroup("local.test")) == [1]
        assert list(nntp_client.listgroup("local.test", (1, 1))) == [1]


def test_ihave() -> None:
    message_id = f"<ihave.{os.getpid()}.{time.time_ns()}@example.com>"
    headers = {
        "From": "a@example.com",
        "Newsgroups": "local.test",
        "Subject": "IHAVE test",
        "Path": "example.com!not-for-mail",
        "Date": email.utils.formatdate(),
    }
    with nntp.NNTPClient("localhost") as nntp_client:
        capabilities = list(nntp_client.capabilities())
        if "IHAVE" not in capabilities:
            pytest.skip("The server does not offer IHAVE to this client")
        nntp_client.ihave(message_id, headers, "IHAVE body\n")
        with pytest.raises(nntp.NNTPTemporaryError) as info:
            nntp_client.ihave(message_id, headers, "IHAVE body\n")
        assert info.value.code == 435  # Duplicate: not wanted
        assert nntp_client.stat(message_id)[1] == message_id


def test_compress() -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        if "COMPRESS DEFLATE" not in list(nntp_client.capabilities()):
            pytest.skip("The server does not offer COMPRESS DEFLATE")
        nntp_client.compress()
        assert "COMPRESS DEFLATE" not in list(nntp_client.capabilities())
        nntp_client.post(
            {"From": "a@example.com", "Newsgroups": "local.test", "Subject": "zip"},
            "compressed body\n" * 100,
        )
        _, first, last, _ = nntp_client.group("local.test")
        overviews = list(nntp_client.xover((first, last)))
        number = next(n for n, o in overviews if o["subject"] == "zip")
        assert nntp_client.body(number) == b"compressed body\r\n" * 100
        assert nntp_client.date().year >= 2024
        with pytest.raises(nntp.NNTPError, match="not allowed after COMPRESS"):
            nntp_client.mode_reader()
