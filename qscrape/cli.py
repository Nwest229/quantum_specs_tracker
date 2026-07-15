"""Command-line entry point.

    python -m qscrape --config config/sources.json --out data/backends.json

Flags:
    --config PATH   source registry (default config/sources.json)
    --out PATH      output array (default data/backends.json)
    --no-cache      force live re-fetch of every source
    --only VENDOR   restrict to one vendor key (repeatable)
    --quiet         suppress the per-run summary
    --xlsx [PATH]   also write an Excel workbook (default data/quantum_tracker.xlsx)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .httpcache import HttpCache
from .pipeline import Pipeline

ROOT = os.path.dirname(os.path.dirname(__file__))
_SENTINEL = object()


def _filter_config(config: dict, only: list[str]) -> dict:
    if not only:
        return config
    only = {o.lower() for o in only}
    config = dict(config)
    config["api_sources"] = {k: v for k, v in config.get("api_sources", {}).items()
                             if k.lower() in only}
    config["spec_sources"] = [e for e in config.get("spec_sources", [])
                              if e.get("vendor", "").lower() in only]
    return config


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="qscrape",
                                description="Build the combined quantum-backend JSON array.")
    p.add_argument("--config", default=os.path.join(ROOT, "config", "sources.json"))
    p.add_argument("--out", default=os.path.join(ROOT, "data", "backends.json"))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--xlsx", nargs="?", const=_SENTINEL, default=None,
                   help="write an .xlsx workbook (optional path; default data/quantum_tracker.xlsx)")
    args = p.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        config = _filter_config(json.load(fh), args.only)

    http = HttpCache(max_age=0 if args.no_cache else config.get("cache_max_age", 86400),
                     delay=config.get("request_delay", 1.0))
    pipe = Pipeline(config, http=http)
    docs = pipe.run()
    pipe.write(docs, args.out)

    xlsx_path = None
    if args.xlsx is not None:
        from .export_xlsx import write_workbook
        xlsx_path = (os.path.join(ROOT, "data", "quantum_tracker.xlsx")
                     if args.xlsx is _SENTINEL else args.xlsx)
        write_workbook(docs, xlsx_path)

    rep = pipe.report
    if not args.quiet:
        print(f"backends: {rep['counts'].get('backends', 0)}  "
              f"vendors: {rep['counts'].get('vendors', 0)}")
        print(f"warnings: {len(rep['warnings'])}  "
              f"validation errors: {len(rep['validation_errors'])}")
        for w in rep["warnings"][:20]:
            print("  warn:", w)
        for e in rep["validation_errors"][:20]:
            print("  invalid:", e["backend"], e["path"], "-", e["message"])
        print(f"wrote {args.out}")
        if xlsx_path:
            print(f"wrote {xlsx_path}")
    return 1 if rep["validation_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
