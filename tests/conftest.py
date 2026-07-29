"""Shared pytest fixtures/helpers for offline unit tests."""

from __future__ import annotations

import pytest

from qscrape.httpcache import Response


class FakeHttp:
    """Serves a canned page for any URL so tests never touch the network."""

    def __init__(
        self,
        text: str,
        status: int = 200,
        content_type: str = "text/html",
        retrieved: str = "2026-07-06T00:00:00+00:00",
    ) -> None:
        self._text, self._status, self._ct, self._ret = text, status, content_type, retrieved

    def get(self, url: str, headers: dict[str, str] | None = None, force: bool = False) -> Response:
        return Response(
            url=url,
            status=self._status,
            text=self._text,
            retrieved=self._ret,
            from_cache=False,
            content_type=self._ct,
        )


@pytest.fixture
def fake_http_factory() -> type[FakeHttp]:
    """Returns the FakeHttp class so tests can construct instances with
    per-test text/status/content_type."""
    return FakeHttp
