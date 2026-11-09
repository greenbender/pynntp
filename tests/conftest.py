"""Fixtures for the client tests: a scripted fake socket per test."""

from __future__ import annotations

import pytest

import nntp
from tests.helpers import Connect, FakeSocket


@pytest.fixture(params=[1, 7, 4096], ids=lambda c: f"chunk{c}")
def chunk(request: pytest.FixtureRequest) -> int:
    """Run each test with several recv sizes to exercise buffer boundaries."""
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch, chunk: int) -> Connect:
    """Return a factory: connect(server_bytes) -> (client, fake_socket).

    Extra positional byte strings are served to later connections, made by
    reconnect().
    """

    def factory(
        server: bytes, *later: bytes, **kwargs: object
    ) -> tuple[nntp.NNTPClient, FakeSocket]:
        socks = [FakeSocket(data, chunk) for data in (server, *later)]
        monkeypatch.setattr(
            "nntp.nntp.socket.create_connection", lambda *_, **__: socks.pop(0)
        )
        sock = socks[0]
        return nntp.NNTPClient("example.com", **kwargs), sock  # type: ignore[arg-type]

    return factory
