import ssl

import pytest

import nntp

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
    """
    https://docs.python.org/3/reference/datamodel.html#context-managers
    """
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@pytest.mark.xfail(reason="[Errno 9] Bad file descriptor", raises=OSError, strict=True)
def test_context_manager_on_close() -> None:
    """
    nntp_client.close() should normally not be called within the context manager.
    """
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS
        nntp_client.close()


@pytest.mark.xfail(reason="[Errno 9] Bad file descriptor", raises=OSError, strict=True)
def test_context_manager_on_quit() -> None:
    """
    nntp_client.quit() should normally not be called within the context manager.
    """
    with nntp.NNTPClient("localhost") as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS
        nntp_client.quit()


def test_nntp_client_without_ssl() -> None:
    with nntp.NNTPClient("localhost", use_ssl=False) as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@pytest.mark.xfail(
    reason="INN2 in not configured to support SSL",
    raises=ssl.SSLError,
    strict=True,
)
def test_nntp_client_with_ssl() -> None:
    with nntp.NNTPClient("localhost", use_ssl=True) as nntp_client:
        newsgroups = set(nntp_client.list_newsgroups())
        assert newsgroups == DEFAULT_NEWGROUPS


@pytest.mark.parametrize(
    "newsgroup",
    [
        "local.general",
        "local.test",
        pytest.param(
            "junk",
            marks=pytest.mark.xfail(raises=nntp.nntp.NNTPTemporaryError, strict=True),
        ),
    ],
)
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
            assert low == 0 if newsgroup == "junk" else 1
            assert high == 1
            assert status == "n" if newsgroup == "junk" else "y"


@pytest.mark.parametrize(
    "newsgroup",
    [
        "local.general",
        "local.test",
        pytest.param(
            "junk",
            marks=pytest.mark.xfail(raises=nntp.nntp.NNTPTemporaryError, strict=True),
        ),
    ],
)
def test_article(newsgroup: str) -> None:
    with nntp.NNTPClient("localhost") as nntp_client:
        total, first, last, group = nntp_client.group(newsgroup)
        assert total == 0 if newsgroup == "junk" else 1
        assert first == 1
        assert last == 0 if newsgroup == "junk" else 1
        assert group == newsgroup
        article_number, headers, body = nntp_client.article()
        assert article_number == 1
        assert headers["Newsgroups"] == newsgroup
        assert headers["Subject"] == f"Test post to {newsgroup}"
        assert body == f"This is a test post to {newsgroup}\r\n".encode("utf-8")
