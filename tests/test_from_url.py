"""NNTPClient.from_url(): URL schemes, credentials and validation."""

from __future__ import annotations

import inspect
import ssl
from typing import Any

import pytest

import nntp
from nntp import SSLMode
from nntp.nntp import FromURLOptions
from tests.helpers import DATE, WELCOME, FakeSocket

TIMEOUT = 5.5


@pytest.fixture
def init_args(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the arguments from_url() passes to the constructor."""
    recorded: dict[str, Any] = {}

    def record(_self: nntp.NNTPClient, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        names = ("host", "port", "username", "password")
        recorded.update(zip(names, args, strict=False), **kwargs)

    monkeypatch.setattr(nntp.NNTPClient, "__init__", record)
    return recorded


@pytest.mark.parametrize(
    ("url", "host", "port", "use_ssl", "ssl_mode"),
    [
        ("nntp://news.example.com", "news.example.com", None, False, SSLMode.IMPLICIT),
        ("nntp://news.example.com/", "news.example.com", None, False, SSLMode.IMPLICIT),
        (
            "nntps://news.example.com:1563",
            "news.example.com",
            1563,
            True,
            SSLMode.IMPLICIT,
        ),
        ("snews://news.example.com", "news.example.com", None, True, SSLMode.IMPLICIT),
        (
            "NNTP+STARTTLS://News.Example.com",
            "news.example.com",
            None,
            True,
            SSLMode.STARTTLS,
        ),
        ("nntp://[::1]:119", "::1", 119, False, SSLMode.IMPLICIT),
    ],
)
def test_schemes(
    init_args: dict[str, Any],
    url: str,
    host: str,
    port: int | None,
    use_ssl: bool,
    ssl_mode: SSLMode,
) -> None:
    nntp.NNTPClient.from_url(url)
    assert (init_args["host"], init_args["port"]) == (host, port)
    assert (init_args["use_ssl"], init_args["ssl_mode"]) == (use_ssl, ssl_mode)
    # Nothing else was set, so the constructor's defaults apply
    assert set(init_args) == {"host", "port", "use_ssl", "ssl_mode"}


@pytest.mark.parametrize(
    ("url", "kwargs", "credentials"),
    [
        ("nntp://fred:p%40ss%3A@h", {}, ("fred", "p@ss:")),  # User info, decoded
        ("nntp://fred:a+b@h", {}, ("fred", "a+b")),  # "+" is not a space
        ("nntp://h", {"username": "fred", "password": "pw"}, ("fred", "pw")),  # Args
        # Arguments win over the URL, per field
        ("nntp://fred:pw@h", {"password": "kpw"}, ("fred", "kpw")),
        ("nntp://fred@h", {"username": "k", "password": "kpw"}, ("k", "kpw")),
        ("nntp://fred:pw@h", {"username": "", "password": ""}, ("", "")),
    ],
)
def test_credentials(
    init_args: dict[str, Any],
    url: str,
    kwargs: FromURLOptions,
    credentials: tuple[str, str],
) -> None:
    nntp.NNTPClient.from_url(url, **kwargs)
    assert (init_args["username"], init_args["password"]) == credentials


def test_other_arguments_pass_through(init_args: dict[str, Any]) -> None:
    context = ssl.create_default_context()
    nntp.NNTPClient.from_url(
        "nntps://h", timeout=TIMEOUT, ssl_context=context, reader=False
    )
    assert init_args["timeout"] == TIMEOUT
    assert init_args["ssl_context"] is context
    assert init_args["reader"] is False


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://news.example.com", "Unsupported URL scheme"),
        ("news.example.com", "Unsupported URL scheme"),
        ("nntp://", "has no host"),
        ("nntp://h/alt.test", "path or fragment"),
        ("nntp://h#frag", "path or fragment"),
        ("nntp://h:0", "port may not be 0"),
        ("nntp://h?group=alt.test", "Unsupported URL query parameter"),
        ("nntp://h?username=fred", "Unsupported URL query parameter"),
        ("nntp://h?password=pw", "Unsupported URL query parameter"),
        ("nntp://h?timeout=1&timeout=2", "Repeated URL query parameter"),
        ("nntp://h?timeout=abc", "Invalid URL timeout"),
        ("nntp://h?timeout=0", "Invalid URL timeout"),
        ("nntp://h?timeout=-1", "Invalid URL timeout"),
        ("nntp://h?timeout=inf", "Invalid URL timeout"),
        ("nntp://h?timeout=nan", "Invalid URL timeout"),
        ("nntp://h?timeout=", "Invalid URL timeout"),
        ("nntp://h?reader=maybe", "Invalid URL reader"),
        ("nntp://h:99999", "out of range"),
    ],
)
def test_invalid_urls(init_args: dict[str, Any], url: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        nntp.NNTPClient.from_url(url)
    assert init_args == {}


def test_connects_and_logs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    server = WELCOME + b"480 auth\r\n381 pass\r\n281 ok\r\n" + DATE
    sock = FakeSocket(server, 4096)
    addresses = []

    def create_connection(address: tuple[str, int], **_: object) -> FakeSocket:
        addresses.append(address)
        return sock

    monkeypatch.setattr("nntp.nntp.socket.create_connection", create_connection)
    client = nntp.NNTPClient.from_url("nntp://fred:p%40ss@example.com:1119")
    assert addresses == [("example.com", 1119)]
    assert client.date().year == 2024
    assert b"AUTHINFO USER fred\r\nAUTHINFO PASS p@ss\r\n" in sock.sent


def test_accepts_every_constructor_argument_not_in_the_url() -> None:
    # host, port, use_ssl and ssl_mode come from the URL; everything else must
    # be accepted by from_url() too, so new constructor arguments aren't missed
    options = set(FromURLOptions.__annotations__)
    init = set(inspect.signature(nntp.NNTPClient.__init__).parameters)
    assert options == init - {"self", "host", "port", "use_ssl", "ssl_mode"}


@pytest.mark.parametrize(
    ("query", "timeout"),
    [
        ("timeout=10", 10.0),
        ("timeout=2.5", 2.5),
        ("timeout=none", None),
        ("timeout=NONE", None),
    ],
)
def test_timeout_from_url(
    init_args: dict[str, Any], query: str, timeout: float | None
) -> None:
    nntp.NNTPClient.from_url(f"nntp://h?{query}")
    assert init_args["timeout"] == timeout


@pytest.mark.parametrize(
    ("value", "reader"),
    [
        ("false", False),
        ("0", False),
        ("No", False),
        ("off", False),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_reader_from_url(init_args: dict[str, Any], value: str, reader: bool) -> None:
    nntp.NNTPClient.from_url(f"nntp://h?reader={value}")
    assert init_args["reader"] is reader


def test_arguments_override_url_options(init_args: dict[str, Any]) -> None:
    url = "nntp://h?timeout=10&reader=true"
    nntp.NNTPClient.from_url(url, timeout=TIMEOUT, reader=False)
    assert (init_args["timeout"], init_args["reader"]) == (TIMEOUT, False)


def test_url_is_validated_even_when_overridden(init_args: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Invalid URL timeout"):
        nntp.NNTPClient.from_url("nntp://h?timeout=abc", timeout=TIMEOUT)
    assert init_args == {}  # Not constructed


@pytest.mark.parametrize(
    "url",
    [
        "nntps://fred:s3cret@h/path",
        "nntps://fred:s3cret@h#frag",
        "nntps://fred:s3cret@h?timeout=1&timeout=2",
        "nntps://fred:s3cret@h?nope=1",
        "nntps://fred:s3cret@h?timeout=abc",
    ],
)
def test_errors_never_show_the_password(init_args: dict[str, Any], url: str) -> None:
    with pytest.raises(ValueError) as info:  # noqa: PT011
        nntp.NNTPClient.from_url(url)
    assert "s3cret" not in str(info.value)
    assert init_args == {}
