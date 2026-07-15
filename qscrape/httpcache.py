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
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

DEFAULT_CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
UA = "elQtron-quantum-db/0.1 (+research; contact via repo)"


@dataclass
class Response:
    url: str
    status: int
    text: str
    retrieved: str          # ISO-8601, UTC
    from_cache: bool
    content_type: str = ""

    def json(self):
        return json.loads(self.text)


class HttpCache:
    def __init__(self, cache_dir: str = DEFAULT_CACHE, max_age: float = 86400,
                 delay: float = 1.0, timeout: float = 30.0):
        self.cache_dir = cache_dir
        self.max_age = max_age          # seconds; 0 disables cache reads
        self.delay = delay              # politeness delay between live fetches
        self.timeout = timeout
        self._last_fetch = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, url: str) -> str:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.cache_dir, h + ".json")

    def _read_cache(self, url: str) -> Optional[Response]:
        p = self._path(url)
        if not os.path.exists(p):
            return None
        if self.max_age and (time.time() - os.path.getmtime(p)) > self.max_age:
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        return Response(url=d["url"], status=d["status"], text=d["text"],
                        retrieved=d["retrieved"], from_cache=True,
                        content_type=d.get("content_type", ""))

    def _write_cache(self, resp: Response) -> None:
        p = self._path(resp.url)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "url": resp.url, "status": resp.status, "text": resp.text,
                "retrieved": resp.retrieved, "content_type": resp.content_type,
            }, fh)
        os.replace(tmp, p)

    def get(self, url: str, headers: Optional[dict] = None,
            force: bool = False) -> Response:
        if not force:
            cached = self._read_cache(url)
            if cached is not None:
                return cached

        # politeness throttle
        wait = self.delay - (time.time() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)

        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                charset = r.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                resp = Response(url=url, status=r.status, text=text,
                                retrieved=retrieved, from_cache=False,
                                content_type=r.headers.get_content_type())
        except urllib.error.HTTPError as e:
            resp = Response(url=url, status=e.code, text="", retrieved=retrieved,
                            from_cache=False)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            resp = Response(url=url, status=0, text=f"__fetch_error__: {e}",
                            retrieved=retrieved, from_cache=False)
        finally:
            self._last_fetch = time.time()

        if resp.status == 200 and not resp.text.startswith("__fetch_error__"):
            self._write_cache(resp)
        return resp
