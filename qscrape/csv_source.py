"""Importer for the eleQtron market-analysis CSV (the historical baseline).

The market-analysis sheet is exported as a German-locale, semicolon-delimited
CSV: comma decimals (``99,95%``, ``0,022``), dot thousands in money cells
(``135.000,00 €``), ``#DIV/0!`` / ``n/a`` / ``-`` sentinels, and the odd
multi-line quoted source cell. Its columns are exactly the tracker schema, so
each row maps cleanly to a :class:`BackendRecord` — carrying the row's own
Tech/Price sources and dates as provenance.

This is the *baseline* tier: it runs after the live adapters, so a live-scraped
value wins for a system the scraper also covers, while every historical row the
scraper doesn't have (the 2022 IBM Falcon fleet, the eleQtron roadmap, AQT,
Oxford Ionics, …) is added intact.

Prices: the ``(ct)`` columns are US cents → stored as ``USD <dollars>``; the
``€`` columns are stored as ``EUR <amount>``. Currencies are never silently
mixed downstream.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
from typing import Any, Iterable, Optional

from .models import BackendRecord, F, UNKNOWN

# ---- column layout (0-indexed, from the sheet's header row) ----------------
COL = {
    "id": 0, "type": 1, "vendor": 2, "model": 3, "system_name": 4,
    "planned_release": 5, "commercial_release": 6,
    "black_box": 7, "argmax": 8, "vendor_metric": 9, "theo_max": 10, "b": 11,
    "qubits": 12, "topo_type": 13, "edges": 14,
    "2q_max": 15, "2q_avg": 16, "2q_min": 17, "1q_max": 18, "1q_avg": 19,
    "1q_min": 20, "spam_avg": 21,
    "1q_gate_time_s": 22, "2q_gate_time_s": 23, "readout_time_s": 24,
    "shot_rate_min": 25, "shot_rate_avg": 26, "shot_rate_max": 27,
    "clops": 28, "credits_per_hour": 29,
    "tech_date": 30, "tech_source": 31,
    "mid_circuit_measurement": 32, "conditional_logic": 33, "parallel_2q": 34,
    "qubit_reuse": 35, "hybrid_execution": 36, "uptime": 37,
    "per_1q_gate": 38, "per_2q_gate": 39, "per_iteration": 40, "per_shot": 41,
    "per_task": 42, "per_second": 43, "per_hour": 44, "per_month": 45,
    "per_system": 46,
    "price_date": 47, "price_source": 48, "comment": 49,
}

_SENTINELS = {"", "-", "n/a", "#div/0!", "?", "*"}

# terse Type codes -> readable system class
_TYPE_MAP = {
    "sc": "gate-based (superconducting)",
    "sc-qa": "quantum annealer (superconducting)",
    "scit-l": "gate-based (superconducting)",
    "it-mw": "gate-based (trapped-ion, microwave)",
    "it-l": "gate-based (trapped-ion, laser)",
    "na": "analog/gate (neutral-atom)",
    "qem": "emulator / simulator",
}


def _clean(x: Any) -> str:
    return (x or "").strip()


def _known(x: str) -> bool:
    return _clean(x).lower() not in _SENTINELS


def de_num(text: str) -> Optional[float]:
    """German tech number: comma is the decimal point, supports E notation.
    '1,5E+05'->150000, '2,5E-08'->2.5e-08, '364,35'->364.35, '99,14%'->99.14."""
    s = _clean(text)
    if not _known(s):
        return None
    s = s.replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
        return float(m.group()) if m else None


def de_percent(text: str) -> Optional[float]:
    """'99,95%' -> 0.9995 ; '99,5' -> 0.995 ; '100,00%' -> 1.0."""
    n = de_num(text)
    if n is None:
        return None
    return n / 100.0 if (n > 1.0 or "%" in str(text)) else n


def de_money(text: str) -> Optional[float]:
    """German money: dot thousands + comma decimal, strip currency.
    '135.000,00 €'->135000.0 ; '1,60 €'->1.60 ; '0,30'->0.30."""
    s = _clean(text)
    if not _known(s):
        return None
    s = re.sub(r"[€$£\sA-Za-z]", "", s)
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _decimal_str(val: float) -> str:
    """Plain-decimal string, no scientific notation, no trailing zeros.
    0.00003 -> '0.00003', 135000.0 -> '135000', 0.0 -> '0'."""
    if val == 0:
        return "0"
    s = f"{val:.12f}".rstrip("0").rstrip(".")
    return s or "0"


def de_date(text: str) -> str:
    """'31.07.2024' -> '2024-07-31'. Returns '' if unparseable."""
    s = _clean(text)
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _source_kind(url: str) -> str:
    u = (url or "").lower()
    if "arxiv" in u or "doi.org" in u or "journals" in u or "prx" in u:
        return "publication"
    if "aws.amazon" in u or "azure" in u or "learn.microsoft" in u:
        return "cloud-api"
    if "wikipedia" in u or "quantumcomputingreport" in u or "venturebeat" in u \
            or "linkedin" in u:
        return "third-party"
    return "vendor"


def _backend_name(row) -> str:
    vendor = re.sub(r"\s+", " ", _clean(row[COL["vendor"]]))
    sysname = _clean(row[COL["system_name"]])
    model = _clean(row[COL["model"]])
    name = sysname if _known(sysname) else (model if _known(model) else "")
    name = _clean(name)
    # Cloud device ids like "ionq_forte" / "ibmq_washington" -> drop the leading
    # vendor token and title-case the rest, so CSV rows align with the live
    # scraper's clean product names ("IonQ Forte") and stay readable ("Washington").
    if "_" in name and name == name.lower():
        name = name.split("_", 1)[1].replace("_", " ").strip().title()
    if not name:
        name = "system"
    base = vendor.split(" (")[0]  # drop "(Team)"/"(EQO)" qualifiers for the label
    if base and base.lower() not in name.lower():
        name = f"{base} {name}"
    return name


def rows_from_csv(text: str) -> tuple[list[list[str]], list[str]]:
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    hi = next((i for i, r in enumerate(rows) if r and _clean(r[0]) == "ID"), None)
    if hi is None:
        return [], []
    header = rows[hi]
    data = [r for r in rows[hi + 1:] if r and _clean(r[0]).isdigit()]
    return data, header


def records_from_csv(path: str) -> Iterable[BackendRecord]:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        data, _ = rows_from_csv(fh.read())

    for row in data:
        # pad short rows so index access is always safe
        if len(row) < len(COL):
            row = row + [""] * (len(COL) - len(row))
        vendor = re.sub(r"\s+", " ", _clean(row[COL["vendor"]]))
        if not _known(vendor):
            continue  # blank/placeholder row
        if vendor.lower().startswith("eleqtron"):
            continue  # tracker holds competitors only; eleQtron rows are excluded
        yield _record(row, vendor)


def _record(row, vendor: str) -> BackendRecord:
    rec = BackendRecord(backend_name=_backend_name(row), vendor=vendor.lower())
    rec.id = _clean(row[COL["id"]])  # honour the sheet's own ID
    tcode = _clean(row[COL["type"]]).lower()
    rec.type = _TYPE_MAP.get(tcode, _clean(row[COL["type"]]) or "")
    if _known(row[COL["model"]]):
        rec.model = _clean(row[COL["model"]])
    if _known(row[COL["system_name"]]):
        rec.system_name = _clean(row[COL["system_name"]])
    if _known(row[COL["planned_release"]]):
        rec.planned_release = _clean(row[COL["planned_release"]])
    if _known(row[COL["commercial_release"]]):
        rec.commercial_release = _clean(row[COL["commercial_release"]])

    tsrc = _clean(row[COL["tech_source"]])
    tdate = de_date(row[COL["tech_date"]])
    tkind = _source_kind(tsrc)
    psrc = _clean(row[COL["price_source"]])
    pdate = de_date(row[COL["price_date"]])
    pkind = _source_kind(psrc)

    def put(path, raw, conv, method, kind=tkind, src=tsrc, ret=tdate, unit=""):
        if not _known(raw):
            return
        val = conv(raw)
        if val is None:
            return
        if unit:                       # money: currency-tagged plain-decimal string
            val = f"{unit} {_decimal_str(val)}"
        rec.set(path, F(val, src if _known(src) else "", ret, method, kind))

    # headline metrics. In the sheet, column 'b' (COL['b']) is the Algorithmic
    # Qubits count — which is exactly what our black_box field means — while
    # Argmax/Vendor are the vendor's own headline figures kept verbatim.
    aq = de_num(row[COL["b"]])
    if aq is not None:
        rec.black_box = F(int(aq) if aq == int(aq) else aq,
                          tsrc if _known(tsrc) else "", tdate, "vendor-spec", tkind)
    for name in ("argmax", "vendor_metric"):
        raw = _clean(row[COL[name]])
        if _known(raw):
            setattr(rec, name, F(raw, tsrc if _known(tsrc) else "", tdate,
                                 "vendor-spec", tkind))
    # the sheet's "Black-box" QV-style benchmark (col 7) is a distinct concept;
    # preserve it in meta rather than overwrite the AQ field.
    bb = _clean(row[COL["black_box"]])
    if _known(bb):
        rec.meta["csv_black_box_benchmark"] = bb

    # topology
    put("qpu_topology.qubits", row[COL["qubits"]], lambda x: int(de_num(x)), "vendor-spec")
    if _known(row[COL["topo_type"]]):
        rec.set("qpu_topology.type",
                F(_clean(row[COL["topo_type"]]), tsrc if _known(tsrc) else "", tdate, "vendor-spec", tkind))
    put("qpu_topology.edges", row[COL["edges"]], lambda x: int(round(de_num(x))), "vendor-spec")

    # fidelity (percent -> fraction)
    for k in ("2q_max", "2q_avg", "2q_min", "1q_max", "1q_avg", "1q_min", "spam_avg"):
        put(f"fidelity.{k}", row[COL[k]], de_percent,
            "maximum" if "max" in k else ("minimum" if "min" in k else "average"))

    # operation speed (numbers / seconds / Hz)
    for k in ("1q_gate_time_s", "2q_gate_time_s", "readout_time_s",
              "shot_rate_min", "shot_rate_avg", "shot_rate_max", "clops",
              "credits_per_hour"):
        put(f"operation_speed.{k}", row[COL[k]], de_num, "vendor-spec")

    # features (verbatim strings: yes/no/counts)
    for k in ("mid_circuit_measurement", "conditional_logic", "parallel_2q",
              "qubit_reuse", "hybrid_execution", "uptime"):
        raw = _clean(row[COL[k]])
        if _known(raw):
            rec.set(f"features.{k}", F(raw, tsrc if _known(tsrc) else "", tdate, "vendor-spec", tkind))

    # pricing: (ct) cols are US cents -> USD dollars ; € cols -> EUR
    for k in ("per_1q_gate", "per_2q_gate", "per_iteration", "per_shot", "per_task"):
        put(f"pricing.{k}", row[COL[k]], lambda x: de_num(x) / 100.0,
            "vendor-spec", kind=pkind, src=psrc, ret=pdate, unit="USD")
    for k in ("per_second", "per_hour", "per_month", "per_system"):
        put(f"pricing.{k}", row[COL[k]], de_money,
            "vendor-spec", kind=pkind, src=psrc, ret=pdate, unit="EUR")
    if _known(row[COL["comment"]]):
        rec.set("pricing.comments",
                F(_clean(row[COL["comment"]]), psrc if _known(psrc) else "", pdate, "note", pkind))

    # preserve the sheet's own precomputed theoretical-max headline for reference
    tm = de_num(row[COL["theo_max"]])
    if tm is not None and tm > 0:
        rec.meta["csv_theo_max"] = tm
        rec.meta.setdefault("csv_theo_max_log2", round(math.log2(tm), 3))
    rec.meta["csv_id"] = _clean(row[COL["id"]])
    rec.meta["tier"] = "csv-baseline"
    return rec
