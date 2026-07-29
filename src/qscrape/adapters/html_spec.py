"""Config-driven extractor for vendor spec pages (HTML or JSON).

Most quantum vendors (Quantinuum, IQM, OQC, Pasqal, QuEra, Alice&Bob, ...)
publish specs as marketing HTML or PDFs, not APIs. Rather than hard-code
brittle selectors in Python, each backend is described declaratively in
``config/sources.json``:

    {
      "vendor": "quantinuum",
      "tier": "vendor",
      "backend_name": "Quantinuum H2-1",
      "meta": {"system_name": "H2", "model": "H2-1"},
      "url": "https://www.quantinuum.com/...",
      "fields": {
        "qpu_topology.qubits":   {"regex": "(\\d+)\\s+qubits", "method": "vendor-spec"},
        "fidelity.2q_avg":       {"selector": ".spec-2q .value", "as": "fraction",
                                  "method": "average"},
        "quantum_volume":        {"regex": "Quantum Volume[^0-9]*([0-9,]+)",
                                  "as": "int", "method": "measured"}
      }
    }

Extraction rule keys:
    selector   CSS selector (first match's text) -- requires bs4
    regex      first capture group of a regex over the raw page text
    json_path  dotted path into a parsed JSON document (e.g. "data.0.fidelity")
    const      a literal value (for values only in a PDF/press release; pair with
               a 'source' so provenance is honest)
    as         coercion: "int" | "float" | "fraction" | "percent" | "str" (default str)
    method     statistic kind stored on the Field
    source     override source URL (else the page url is used)
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from ..models import BackendRecord, Field, F, UNKNOWN, _SCALAR_META
from ..normalize import parse_number, parse_percent_as_fraction
from .base import Adapter

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAVE_BS4 = True
except ImportError:  # pragma: no cover
    _HAVE_BS4 = False


def _coerce(raw: Any, how: str):
    if raw is None:
        return None
    how = how or "str"
    if how == "int":
        n = parse_number(raw)
        return int(n) if n is not None else None
    if how == "float":
        return parse_number(raw)
    if how == "fraction":
        return parse_percent_as_fraction(raw)
    if how == "fraction_complement":
        # an ERROR rate quoted as a percent/number -> fidelity (1 - error)
        e = parse_percent_as_fraction(raw)
        return None if e is None else round(1.0 - e, 6)
    if how == "percent":
        n = parse_number(raw)
        return n / 100.0 if (n is not None and n > 1) else n
    return str(raw).strip()


def _json_path(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


class HtmlSpecAdapter(Adapter):
    """Instantiated once per backend entry in the config."""

    def __init__(self, http, config: dict):
        super().__init__(http, config)
        self.vendor = config.get("vendor", "")
        self.tier = config.get("tier", "vendor")

    def fetch(self) -> Iterable[BackendRecord]:
        cfg = self.config
        rec = BackendRecord(backend_name=cfg["backend_name"], vendor=self.vendor)
        for k in _SCALAR_META:
            if k in cfg.get("meta", {}):
                setattr(rec, k, cfg["meta"][k])

        # Only hit the network if some rule actually reads the page. Pure-const
        # entries (values verified by hand, each with its own source) don't.
        needs_page = any(any(k in rule for k in ("regex", "selector", "json_path"))
                         for rule in cfg.get("fields", {}).values())
        url = cfg.get("url", "")
        page_text = ""
        soup = None
        doc = None
        retrieved = ""
        fetch_ok = False
        if url and needs_page:
            resp = self.http.get(url)
            retrieved = resp.retrieved
            if resp.status != 200:
                self.warn(rec, f"fetch failed for {url}: status={resp.status}")
            else:
                fetch_ok = True
                page_text = resp.text
                if resp.content_type == "application/json" or cfg.get("json"):
                    try:
                        doc = json.loads(page_text)
                    except json.JSONDecodeError:
                        self.warn(rec, "declared JSON but body did not parse")
                elif _HAVE_BS4:
                    soup = BeautifulSoup(page_text, "html.parser")

        for dotted, rule in cfg.get("fields", {}).items():
            raw = self._extract(rule, page_text, soup, doc, rec)
            if raw is None and "const" not in rule:
                continue
            is_const = "const" in rule
            value = _coerce(rule.get("const", raw), rule.get("as", "str"))
            if value is None:
                continue
            # A const is a config-asserted fact, not a scrape: only attribute it
            # to the page if that page actually loaded. An explicit rule["source"]
            # always wins. Scraped values are only produced when fetch_ok anyway.
            if rule.get("source"):
                src = rule["source"]
            elif is_const and not fetch_ok:
                src = ""  # unverified assertion -> no source claimed, no sources[] entry
            else:
                src = url
            fld = F(
                value=value,
                source=src,
                retrieved=retrieved or "",
                method=rule.get("method", "vendor-spec"),
                # A const verified against an explicit source carries that
                # source's tier; an unsourced const is a bare assertion.
                kind=("config-asserted" if (is_const and not rule.get("source"))
                      else self.tier),
            )
            rec.set(dotted, fld)

        yield rec

    def _extract(self, rule: dict, text: str, soup, doc, rec: BackendRecord) -> Optional[str]:
        if "const" in rule:
            return rule["const"]
        if "json_path" in rule:
            if doc is None:
                return None
            return _json_path(doc, rule["json_path"])
        if "regex" in rule and text:
            m = re.search(rule["regex"], text, re.IGNORECASE | re.DOTALL)
            if m:
                return m.group(1) if m.groups() else m.group(0)
            return None
        if "selector" in rule:
            if soup is None:
                if not _HAVE_BS4:
                    self.warn(rec, "selector rule needs beautifulsoup4 (not installed)")
                return None
            el = soup.select_one(rule["selector"])
            return el.get_text(strip=True) if el else None
        return None
