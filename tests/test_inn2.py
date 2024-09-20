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
    ("local.general", "Local general discussion group"),
    ("local.test", "Local test group"),
}


def test_nntp_client():
    nntp_client = nntp.NNTPClient("localhost")
    newsgroups = set(nntp_client.list_newsgroups())
    assert newsgroups == DEFAULT_NEWGROUPS


def test_nntp_client_without_ssl():
    nntp_client = nntp.NNTPClient("localhost", use_ssl=False)
    newsgroups = set(nntp_client.list_newsgroups())
    assert newsgroups == DEFAULT_NEWGROUPS


@pytest.mark.xfail(
    reason="INN2 in not configured to support SSL",
    raises=ssl.SSLError,
    strict=True,
)
def test_nntp_client_with_ssl():
    nntp_client = nntp.NNTPClient("localhost", use_ssl=True)
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
def test_post(newsgroup):
    nntp_client = nntp.NNTPClient("localhost")
    headers = {
        "Subject": f"Test post to {newsgroup}",
        "From": "GitHub Actions <actions@github.com>",
        "Newsgroups": newsgroup,
    }
    assert (
        nntp_client.post(headers=headers, body=f"This is a test post to {newsgroup}")
        is True
    )


@pytest.mark.parametrize("newsgroup", ["local.general", "local.test", "junk"])
def test_list_active(newsgroup):
    nntp_client = nntp.NNTPClient("localhost")
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
def test_article(newsgroup):
    nntp_client = nntp.NNTPClient("localhost")
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
