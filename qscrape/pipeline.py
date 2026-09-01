"""Orchestration: run adapters -> normalise -> validate -> merge -> write array.

Produces a *single combined JSON array* of backend records (per the chosen
output format), plus a run report of warnings and validation errors.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import adapters as adp
from .httpcache import HttpCache
from .models import BackendRecord, now_iso, _SCALAR_META
from .normalize import finalize

ROOT = os.path.dirname(os.path.dirname(__file__))
SCHEMA_PATH = os.path.join(ROOT, "schema", "backend.schema.json")

try:
    import jsonschema  # type: ignore
    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAVE_JSONSCHEMA = False


def _record_key(name: str, vendor: str) -> str:
    return f"{vendor}::{name}".lower()


def _is_skip(rec: BackendRecord) -> bool:
    return rec.meta.get("skipped") or rec.backend_name.startswith("__")


# --- change-log -------------------------------------------------------------
# Each run rebuilds backends.json from scratch, so the only way to see *what
# actually changed* (a fidelity ticked up, a price appeared) is to diff the new
# array against the previous one. We ignore provenance/derived noise — only real
# spec values count as a change. Event-based history is provenance-clean, unlike
# a fabricated time-series.
_DIFF_SKIP_TOP = {"sources", "_meta", "derived_metrics", "theoretical_max", "id"}
# provenance leaves carried inline on some fields — a retrieved/source/kind change
# is not a spec change, so never log it
_DIFF_SKIP_LEAF = (".retrieved", ".source", ".kind")


def _flatten(o: Any, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(o, dict):
        for k, v in o.items():
            if not prefix and k in _DIFF_SKIP_TOP:
                continue
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(o, list):
        out[prefix] = json.dumps(o, sort_keys=True, ensure_ascii=False)
    else:
        out[prefix] = o
    return out


def diff_docs(old_docs: list[dict], new_docs: list[dict]) -> tuple[list, list, list]:
    """(changed, added, removed) between two backends.json arrays, keyed by id."""
    def index(docs):
        return {(d.get("id") or d.get("backend_name")): d for d in docs}

    oi, ni = index(old_docs), index(new_docs)
    changed = []
    for key, nd in ni.items():
        if key not in oi:
            continue
        of, nf = _flatten(oi[key]), _flatten(nd)
        for f in sorted(set(of) | set(nf)):
            if f.endswith(_DIFF_SKIP_LEAF):
                continue
            ov, nv = of.get(f), nf.get(f)
            if ov != nv:
                changed.append({"backend": nd.get("backend_name"), "id": key,
                                "field": f, "from": ov, "to": nv})
    added = [{"id": k, "backend": ni[k].get("backend_name")} for k in ni if k not in oi]
    removed = [{"id": k, "backend": oi[k].get("backend_name")} for k in oi if k not in ni]
    return changed, added, removed


def append_changelog(old_docs, new_docs, generated: str, path: str) -> int:
    """Append one run-entry to the change-log iff something changed. Best-effort:
    never raises, so it can't break a pipeline run. Returns #changes logged."""
    try:
        changed, added, removed = diff_docs(old_docs, new_docs)
        if not (changed or added or removed):
            return 0
        log = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                log = json.load(fh)
        log.append({"generated": generated, "changed": changed,
                    "added": added, "removed": removed})
        log = log[-500:]  # bound growth
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(log, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        return len(changed) + len(added) + len(removed)
    except Exception:  # pragma: no cover - change-log is never load-bearing
        return 0


class Pipeline:
    def __init__(self, config: dict, http: HttpCache | None = None):
        self.config = config
        self.http = http or HttpCache(
            max_age=config.get("cache_max_age", 86400),
            delay=config.get("request_delay", 1.0),
        )
        self.records: dict[str, BackendRecord] = {}
        self.report: dict[str, Any] = {"generated": now_iso(), "warnings": [],
                                       "validation_errors": [], "counts": {}}

    # -- collection -------------------------------------------------------
    def run(self) -> list[dict]:
        self._run_api_adapters()
        self._run_spec_adapters()
        self._run_csv_sources()   # baseline: added last so live values win on merge
        for rec in self.records.values():
            finalize(rec)
        docs = [rec.to_dict() for rec in self.records.values()]
        self._validate(docs)
        self.report["counts"] = {
            "backends": len(docs),
            "vendors": len({d["vendor"] for d in docs}),
        }
        return docs

    def _add(self, rec: BackendRecord) -> None:
        if _is_skip(rec):
            for w in rec.meta.get("warnings", []):
                self.report["warnings"].append(f"[{rec.vendor}] {w}")
            return
        key = _record_key(rec.backend_name, rec.vendor)
        if key in self.records:
            self._merge_into(self.records[key], rec)
        else:
            self.records[key] = rec
        for w in rec.meta.get("warnings", []):
            self.report["warnings"].append(f"[{rec.backend_name}] {w}")

    def _run_api_adapters(self) -> None:
        for name, cfg in self.config.get("api_sources", {}).items():
            if not cfg.get("enabled", True):
                continue
            cls = adp.API_ADAPTERS.get(name)
            if cls is None:
                self.report["warnings"].append(f"unknown api adapter '{name}'")
                continue
            for rec in cls(self.http, cfg).fetch():
                self._add(rec)

    def _run_spec_adapters(self) -> None:
        for entry in self.config.get("spec_sources", []):
            for rec in adp.SPEC_ADAPTER(self.http, entry).fetch():
                self._add(rec)

    def _run_csv_sources(self) -> None:
        from .csv_source import records_from_csv
        for entry in self.config.get("csv_sources", []):
            if not entry.get("enabled", True):
                continue
            path = entry.get("path", "")
            if not os.path.isabs(path):
                path = os.path.join(ROOT, path)
            n = 0
            for rec in records_from_csv(path):
                self._add(rec)
                n += 1
            if n == 0:
                self.report["warnings"].append(f"csv source '{path}' yielded no rows")
            else:
                self.report.setdefault("counts", {})["csv_rows"] = \
                    self.report.get("counts", {}).get("csv_rows", 0) + n

    @staticmethod
    def _merge_into(base: BackendRecord, new: BackendRecord) -> None:
        """Combine two records for the same backend, preferring existing known
        values (adapters are ordered by source priority in config)."""
        for grp in ("qpu_topology", "fidelity", "operation_speed", "features", "pricing"):
            bmap = getattr(base, grp)
            for leaf, fld in getattr(new, grp).items():
                if fld.known and not bmap.get(leaf, type(fld)()).known:
                    bmap[leaf] = fld
        for singleton in ("quantum_volume", "black_box", "argmax", "vendor_metric"):
            if getattr(new, singleton).known and not getattr(base, singleton).known:
                setattr(base, singleton, getattr(new, singleton))
        for k in _SCALAR_META:
            if not getattr(base, k) and getattr(new, k):
                setattr(base, k, getattr(new, k))

    # -- validation -------------------------------------------------------
    def _validate(self, docs: list[dict]) -> None:
        if not _HAVE_JSONSCHEMA:
            self.report["warnings"].append("jsonschema not installed; skipped validation")
            return
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            schema = json.load(fh)
        validator = jsonschema.Draft7Validator(schema)
        for doc in docs:
            for err in validator.iter_errors(doc):
                self.report["validation_errors"].append({
                    "backend": doc.get("backend_name"),
                    "path": "/".join(str(p) for p in err.absolute_path),
                    "message": err.message,
                })

    # -- output -----------------------------------------------------------
    def write(self, docs: list[dict], out_path: str) -> None:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        # diff against the previous array before we overwrite it
        old_docs = []
        if os.path.exists(out_path):
            try:
                with open(out_path, encoding="utf-8") as fh:
                    old_docs = json.load(fh)
            except Exception:
                old_docs = []
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(docs, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, out_path)
        # append real spec changes to the change-log (best-effort, never fatal)
        generated = (docs[0].get("_meta", {}).get("generated") if docs else None) or now_iso()
        changelog_path = os.path.join(os.path.dirname(out_path), "changelog.json")
        n = append_changelog(old_docs, docs, generated, changelog_path)
        if n:
            self.report.setdefault("warnings", []).append(f"change-log: {n} change(s) recorded")
        report_path = os.path.join(os.path.dirname(out_path), "run_report.json")
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(self.report, fh, indent=2)
