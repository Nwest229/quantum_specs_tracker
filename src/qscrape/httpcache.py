"""Zero-dependency HTTP fetcher with an on-disk cache.

Uses only the standard library so the pipeline runs with a bare Python 3.9+.
Each fetch records the retrieval timestamp; the cache lets re-runs be
deterministic and polite (no re-hammering vendor sites). Set ``max_age=0`` to
force a fresh fetch.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CACHE = Path(__file__).parent.parent.parent / ".cache"
UA = "elQtron-quantum-db/0.1 (+research; contact via repo)"


@dataclass
class Response:
    url: str
    status: int
    text: str
    retrieved: str  # ISO-8601, UTC
    from_cache: bool
    content_type: str = ""

    def json(self) -> Any:
        return json.loads(self.text)


class HttpCache:
    def __init__(
        self,
        cache_dir: str | Path = DEFAULT_CACHE,
        max_age: float = 86400,
        delay: float = 1.0,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_age = max_age  # seconds; 0 disables cache reads
        self.delay = delay  # politeness delay between live fetches
        self.timeout = timeout
        self._last_fetch = 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / (h + ".json")

    def _read_cache(self, url: str) -> Response | None:
        p = self._path(url)
        if not p.exists():
            return None
        if self.max_age and (time.time() - p.stat().st_mtime) > self.max_age:
            return None
        try:
            with p.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        return Response(
            url=d["url"],
            status=d["status"],
            text=d["text"],
            retrieved=d["retrieved"],
            from_cache=True,
            content_type=d.get("content_type", ""),
        )

    def _write_cache(self, resp: Response) -> None:
        p = self._path(resp.url)
        tmp = p.with_name(p.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "text": resp.text,
                    "retrieved": resp.retrieved,
                    "content_type": resp.content_type,
                },
                fh,
            )
        tmp.replace(p)

    def get(self, url: str, headers: dict[str, str] | None = None, force: bool = False) -> Response:
        if not force:
            cached = self._read_cache(url)
            if cached is not None:
                return cached

        # politeness throttle
        wait = self.delay - (time.time() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)

        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        retrieved = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                charset = r.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                resp = Response(
                    url=url,
                    status=r.status,
                    text=text,
                    retrieved=retrieved,
                    from_cache=False,
                    content_type=r.headers.get_content_type(),
                )
        except urllib.error.HTTPError as e:
            resp = Response(url=url, status=e.code, text="", retrieved=retrieved, from_cache=False)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            resp = Response(
                url=url,
                status=0,
                text=f"__fetch_error__: {e}",
                retrieved=retrieved,
                from_cache=False,
            )
        finally:
            self._last_fetch = time.time()

        if resp.status == 200 and not resp.text.startswith("__fetch_error__"):
            self._write_cache(resp)
        return resp
