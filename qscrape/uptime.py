"""Live availability tracker for quantum devices (qBraid-polled).

The published schedule only shows *planned* availability; real downtime
(unscheduled maintenance, calibration failures) only shows up if you poll the
live status on a timer and log it. This module does exactly that, following the
app's conventions: stdlib-only HTTP (urllib), a CSV append-log under ``data/``,
a static HTML page that reads a JSON report, and cron for scheduling.

Two subcommands (run on the venv Python)::

    python -m qscrape.uptime poll      # one-shot: query qBraid, append a sample,
                                       # refresh data/uptime.json  (cron every 5 min)
    python -m qscrape.uptime report    # rebuild data/uptime.json from the log only

Config: ``config/uptime.json`` (devices + interval + paths) and
``config/uptime_schedule.json`` (the schedule you read off your Resonance
account, entered by hand). ``QBRAID_API_KEY`` comes from the environment. If the
key or device list is missing the poll skips cleanly with a warning — it never
crashes, same as the braket/ibm adapters.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Optional

ROOT = os.path.dirname(os.path.dirname(__file__))
_UA = "elQtron-uptime/0.1 (+research)"

# statuses that mean "the device is usable right now"
_DEFAULT_UP = ("ONLINE",)
# statuses that are our own bookkeeping, not a real device state (excluded from up/down)
_GAP = ("POLL_ERROR",)


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _abspath(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("api_base", "https://api-v2.qbraid.com/api/v1")
    cfg.setdefault("devices_endpoint", "/devices")
    cfg.setdefault("auth_header", "X-API-KEY")
    cfg.setdefault("devices", [])
    cfg.setdefault("exclude_simulators", True)
    cfg.setdefault("up_statuses", list(_DEFAULT_UP))
    cfg.setdefault("log_path", "data/uptime_log.csv")
    cfg.setdefault("report_path", "data/uptime.json")
    cfg.setdefault("schedule_path", "config/uptime_schedule.json")
    return cfg


# ---------------------------------------------------------------------------
# 1. POLL: query qBraid live status
# ---------------------------------------------------------------------------
def _get_devices(cfg: dict, api_key: str) -> list[dict]:
    """GET the device list from qBraid (v2: {api_base}/devices, X-API-KEY header).

    Returns the raw list of device dicts. Raises on transport/HTTP error so the
    caller can record a POLL_ERROR sample instead of a fake device state.
    """
    url = cfg["api_base"].rstrip("/") + "/" + cfg.get("devices_endpoint", "/devices").lstrip("/")
    header = cfg.get("auth_header", "X-API-KEY")
    req = urllib.request.Request(url, headers={header: api_key, "User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", errors="replace"))
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        for k in ("data", "devices", "results", "items"):
            if isinstance(payload.get(k), list):
                return [d for d in payload[k] if isinstance(d, dict)]
        # a dict keyed by id -> list its values if they look like device dicts
        vals = [v for v in payload.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def _dev_id(d: dict):
    """Canonical, stable id for a device — qBraid v2 'qrn' (e.g. 'aws:iqm:qpu:garnet'),
    falling back to 'vrn' / '_id' / name."""
    return d.get("qrn") or d.get("vrn") or d.get("qbraid_id") or d.get("_id") or d.get("name")


def _dev_status(d: dict):
    return d.get("status") or d.get("state") or d.get("availability")


def _dev_ids(d: dict) -> set:
    """All identifiers a config entry might reference, for flexible matching."""
    return {str(d.get(k)) for k in ("qrn", "vrn", "_id", "qbraid_id", "name") if d.get(k)}


def poll_qbraid(cfg: dict, api_key: str, devices: Iterable[str]) -> dict:
    """Return {config_id: {"status":.., "statusMsg":..}} for the configured devices.

    A config entry matches a device if it equals any of the device's identifiers
    (qrn / vrn / _id / name), so you can use whichever id you see in `list`.
    """
    devs = _get_devices(cfg, api_key)
    wanted = list(devices)
    if not wanted:
        return {_dev_id(d): {"status": _dev_status(d), "statusMsg": d.get("statusMsg") or ""} for d in devs}
    out = {}
    for want in wanted:
        match = next((d for d in devs if want in _dev_ids(d)), None)
        out[want] = ({"status": _dev_status(match), "statusMsg": match.get("statusMsg") or ""}
                     if match else {"status": "NOT_FOUND", "statusMsg": "device id not in qBraid response"})
    return out


def append_samples(log_path: str, rows: list[tuple]) -> None:
    """Append (timestamp_utc, device_id, status, status_msg) rows; write header once."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    new = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp_utc", "device_id", "status", "status_msg"])
        w.writerows(rows)


def list_devices(cfg: dict, api_key: str) -> list[dict]:
    """Return the raw device list from qBraid (for discovering device ids)."""
    return _get_devices(cfg, api_key)


# ---------------------------------------------------------------------------
# 1b. NATIVE CROSS-CHECK: qBraid is an aggregator whose normalised status can
#     lag the provider's own API. Poll AWS Braket + IBM directly and compare, so
#     a "qBraid says ONLINE, AWS says OFFLINE" disagreement becomes visible.
#     Everything here is best-effort: missing SDK/creds -> that source is skipped.
# ---------------------------------------------------------------------------
def _native_braket(known_ids: set) -> dict:
    """{qbraid_id: {source, status, up, msg}} from AWS Braket (metadata read = free).
    Only ids also seen on qBraid are kept, so name-normalisation misses drop out."""
    out: dict = {}
    try:
        from braket.aws import AwsDevice  # amazon-braket-sdk
    except Exception:
        return out
    try:
        devs = AwsDevice.get_devices()
    except Exception:
        return out
    for d in devs:
        try:
            prov = (getattr(d, "provider_name", "") or "").lower()
            name = (getattr(d, "name", "") or "").lower().replace(" ", "-")
            st = str(getattr(d, "status", "") or "").upper()   # ONLINE / OFFLINE / RETIRED
            if not prov or not name:
                continue
            qid = f"aws:{prov}:qpu:{name}"
            if known_ids and qid not in known_ids:
                continue
            out[qid] = {"source": "aws-braket", "status": st, "up": st == "ONLINE", "msg": ""}
        except Exception:
            continue
    return out


def _native_ibm(instance: Optional[str]) -> dict:
    """{qbraid_id: {source, status, up, msg}} from IBM's own API (status read = free)."""
    out: dict = {}
    token = os.environ.get("IBM_QUANTUM_TOKEN")
    if not token:
        return out
    try:
        from qiskit_ibm_runtime import QiskitRuntimeService
    except Exception:
        return out
    try:
        kwargs = {"channel": "ibm_quantum_platform", "token": token}
        inst = instance or os.environ.get("IBM_QUANTUM_INSTANCE")
        if inst:
            kwargs["instance"] = inst
        svc = QiskitRuntimeService(**kwargs)
        backends = svc.backends()
    except Exception:
        return out
    for b in backends:
        try:
            st = b.status()
            up = bool(getattr(st, "operational", False))
            short = str(getattr(b, "name", "")).replace("ibm_", "")
            out[f"ibm:ibm:qpu:{short}"] = {
                "source": "ibm", "status": "ONLINE" if up else "OFFLINE",
                "up": up, "msg": getattr(st, "status_msg", "") or ""}
        except Exception:
            continue
    return out


def append_crosscheck(log_path: str, rows: list[tuple]) -> None:
    """Append (ts, device, qbraid_status, native_source, native_status, verdict) rows."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    new = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp_utc", "device_id", "qbraid_status",
                        "native_source", "native_status", "verdict"])
        w.writerows(rows)


def do_crosscheck(cfg: dict, qbraid_key: Optional[str]) -> dict:
    """Poll qBraid + native providers at one instant and compare per device."""
    ts = iso(now_utc())
    cc = cfg.get("crosscheck", {}) or {}
    up_statuses = {s.upper() for s in cfg.get("up_statuses", ["ONLINE"])}

    # fresh qBraid snapshot
    qb: dict = {}
    if qbraid_key:
        try:
            raw = _get_devices(cfg, qbraid_key)
            if cfg.get("exclude_simulators", True):
                raw = [d for d in raw if str(d.get("deviceType", "")).upper() != "SIMULATOR"]
            for d in raw:
                did = _dev_id(d)
                if did:
                    s = _dev_status(d) or "UNKNOWN"
                    qb[did] = {"status": s, "up": str(s).upper() in up_statuses}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            qb = {}

    # native snapshots
    native: dict = {}
    if cc.get("aws_braket", True):
        native.update(_native_braket(set(qb)))
    if cc.get("ibm", True):
        native.update(_native_ibm(cc.get("ibm_instance")))

    devices, log_rows, agree, disagree = [], [], 0, 0
    for did, nv in sorted(native.items()):
        q = qb.get(did)
        if not q:
            continue   # native saw it but qBraid didn't resolve it — can't compare
        ok = (q["up"] == nv["up"])
        agree += ok
        disagree += (not ok)
        devices.append({"device": did, "qbraid": q, "native": nv, "agree": ok})
        log_rows.append((ts, did, q["status"], nv["source"], nv["status"],
                         "agree" if ok else "disagree"))

    result = {
        "generated": ts,
        "sources": sorted({nv["source"] for nv in native.values()}),
        "checked": len(devices), "agree": agree, "disagree": disagree,
        "devices": devices,
    }
    _write_json(_abspath(cfg.get("crosscheck_path", "data/crosscheck.json")), result)
    if log_rows:
        append_crosscheck(_abspath(cfg.get("crosscheck_log_path", "data/crosscheck_log.csv")), log_rows)
    return result


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def do_poll(cfg: dict, api_key: Optional[str]) -> dict:
    ts = iso(now_utc())
    devices = cfg["devices"]
    if not api_key:
        print("uptime: no QBRAID_API_KEY set — skipping poll (nothing logged).")
        return {"polled": 0, "skipped": "no api key"}
    if not devices:
        print("uptime: no devices configured in config/uptime.json — nothing to poll.")
        return {"polled": 0, "skipped": "no devices"}

    # "*" (or "all") tracks every device from the single API response — same one
    # request whether you follow 2 machines or all of them.
    track_all = "*" in devices or devices == ["all"]
    rows = []
    try:
        if track_all:
            raw = _get_devices(cfg, api_key)
            if cfg.get("exclude_simulators", True):
                raw = [d for d in raw if str(d.get("deviceType", "")).upper() != "SIMULATOR"]
            for d in raw:
                did = _dev_id(d)
                if did:
                    rows.append((ts, did, _dev_status(d) or "UNKNOWN", d.get("statusMsg") or ""))
        else:
            result = poll_qbraid(cfg, api_key, devices)
            for did in devices:
                info = result.get(did, {})
                rows.append((ts, did, info.get("status") or "UNKNOWN", info.get("statusMsg") or ""))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        # the poll itself failed — record a gap marker, NOT a device-down state
        for did in (["*"] if track_all else devices):
            rows.append((ts, did, "POLL_ERROR", str(e)[:200]))

    append_samples(_abspath(cfg["log_path"]), rows)
    return {"polled": len(rows), "at": ts,
            "statuses": {r[1]: r[2] for r in rows}}


# ---------------------------------------------------------------------------
# 2. ANALYSE: collapse into intervals + diff against the schedule
# ---------------------------------------------------------------------------
def read_log(log_path: str) -> list[dict]:
    if not os.path.exists(log_path):
        return []
    out = []
    with open(log_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            dt = parse_iso(row.get("timestamp_utc", ""))
            if dt:
                out.append({"ts": dt, "device": row.get("device_id"),
                            "status": (row.get("status") or "").upper(),
                            "msg": row.get("status_msg", "")})
    out.sort(key=lambda r: (r["device"] or "", r["ts"]))
    return out


def collapse_intervals(samples: list[dict], up_statuses: Iterable[str]) -> list[dict]:
    """Merge consecutive non-ONLINE samples into downtime intervals per device.

    A sample is UP if its status is in ``up_statuses``; POLL_ERROR/gap samples
    are dropped (they mean 'we didn't observe', not 'device down'). A downtime
    interval runs from the first DOWN sample to the next UP sample (or the last
    sample seen, if still down).
    """
    up = {s.upper() for s in up_statuses}
    gap = set(_GAP)
    by_dev: dict[str, list[dict]] = {}
    for s in samples:
        if s["status"] in gap:
            continue
        by_dev.setdefault(s["device"], []).append(s)

    intervals = []
    for dev, seq in by_dev.items():
        seq.sort(key=lambda r: r["ts"])
        i = 0
        while i < len(seq):
            if seq[i]["status"] in up:
                i += 1
                continue
            start = seq[i]["ts"]
            statuses = []
            j = i
            while j < len(seq) and seq[j]["status"] not in up:
                statuses.append(seq[j]["status"])
                j += 1
            # end = when it came back up (next UP sample) or last-seen while still down
            end = seq[j]["ts"] if j < len(seq) else seq[j - 1]["ts"]
            ongoing = j >= len(seq)
            intervals.append({
                "device": dev, "start": start, "end": end,
                "duration_s": int((end - start).total_seconds()),
                "statuses": sorted(set(statuses)), "ongoing": ongoing,
            })
            i = j
    intervals.sort(key=lambda iv: (iv["device"], iv["start"]))
    return intervals


def _overlaps(a_start, a_end, b_start, b_end) -> int:
    """Seconds of overlap between [a_start,a_end) and [b_start,b_end)."""
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    return max(0, int((hi - lo).total_seconds())) if hi > lo else 0


def classify(interval: dict, dev_sched: dict) -> str:
    """Classify a downtime interval against the device's schedule:
    'outage' if we have no schedule for this device (can't judge — e.g. machines
    that aren't yours); 'scheduled' if it overlaps planned maintenance; 'off-hours'
    if outside declared availability windows; otherwise 'unscheduled'."""
    if not dev_sched:
        return "outage"
    s, e = interval["start"], interval["end"]
    if e <= s:
        e = s  # zero-length; treat as instantaneous

    for w in dev_sched.get("maintenance", []) or []:
        ws, we = parse_iso(w.get("start")), parse_iso(w.get("end"))
        if ws and we and _overlaps(s, max(e, s), ws, we) > 0:
            return "scheduled"

    windows = dev_sched.get("available_windows") or []
    if windows and not dev_sched.get("available_24_7", False):
        # unscheduled only if the outage intersects a window the device is meant to be up
        end = e if e > s else s
        if not _intersects_weekly(s, end, windows):
            return "off-hours"
    return "unscheduled"


def _intersects_weekly(start: datetime, end: datetime, windows: list) -> bool:
    """Does [start,end) touch any recurring weekly UTC window?
    window = {"days":[0-6 Mon..Sun], "start":"HH:MM", "end":"HH:MM"}. Checked at
    hourly granularity over the (short) interval — good enough for outage tagging."""
    from datetime import timedelta
    cur = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    step = timedelta(minutes=30)
    guard = 0
    while cur <= end and guard < 4000:
        for w in windows:
            days = w.get("days")
            if days is not None and cur.weekday() not in days:
                continue
            hs, he = w.get("start", "00:00"), w.get("end", "23:59")
            cur_hm = cur.strftime("%H:%M")
            if hs <= cur_hm <= he:
                return True
        cur += step
        guard += 1
    return False


def build_report(cfg: dict) -> dict:
    samples = read_log(_abspath(cfg["log_path"]))
    up = cfg["up_statuses"]
    schedule = _load_schedule(_abspath(cfg["schedule_path"]))
    intervals = collapse_intervals(samples, up)

    # per-device summary
    devices = {}
    for s in samples:
        devices.setdefault(s["device"], []).append(s)

    out_devices = []
    total_from = min((s["ts"] for s in samples), default=None)
    total_to = max((s["ts"] for s in samples), default=None)
    for dev in sorted(devices):
        seq = [s for s in devices[dev] if s["status"] not in _GAP]
        latest = max(devices[dev], key=lambda r: r["ts"])
        dev_sched = (schedule.get("devices", {}) or {}).get(dev, {})
        divs = [iv for iv in intervals if iv["device"] == dev]
        for iv in divs:
            iv["class"] = classify(iv, dev_sched)
        down_s = sum(iv["duration_s"] for iv in divs)
        span_s = int((total_to - total_from).total_seconds()) if (total_from and total_to) else 0
        uptime_pct = round(100.0 * (1 - down_s / span_s), 3) if span_s > 0 else None
        out_devices.append({
            "device": dev,
            "current_status": latest["status"],
            "last_seen": iso(latest["ts"]),
            "samples": len(devices[dev]),
            "uptime_pct": uptime_pct,
            "intervals": [_iv_json(iv) for iv in divs],
            "unscheduled": [_iv_json(iv) for iv in divs if iv["class"] == "unscheduled"],
        })

    return {
        "generated": iso(now_utc()),
        "window": {"from": iso(total_from) if total_from else None,
                   "to": iso(total_to) if total_to else None},
        "devices": out_devices,
    }


def _iv_json(iv: dict) -> dict:
    return {"device": iv["device"], "start": iso(iv["start"]), "end": iso(iv["end"]),
            "duration_s": iv["duration_s"], "statuses": iv["statuses"],
            "ongoing": iv.get("ongoing", False), "class": iv.get("class", "unscheduled")}


def _load_schedule(path: str) -> dict:
    if not os.path.exists(path):
        return {"devices": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"devices": {}}


def write_report(cfg: dict, report: dict) -> str:
    path = _abspath(cfg["report_path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="qscrape.uptime",
                                description="Poll live quantum-device availability and analyse downtime.")
    p.add_argument("command", choices=["poll", "report", "list", "crosscheck"],
                   help="poll: query qBraid + append a sample + refresh report; "
                        "report: rebuild report from the log; "
                        "list: print all qBraid devices + ids + live status (to find your device ids); "
                        "crosscheck: poll qBraid + AWS/IBM natively and flag status disagreements")
    p.add_argument("--config", default=os.path.join(ROOT, "config", "uptime.json"))
    p.add_argument("--filter", default="", help="list: substring filter, e.g. --filter iqm")
    p.add_argument("--raw", action="store_true", help="list: dump raw JSON of the first devices (to confirm field names)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    cfg = load_config(args.config)

    if args.command == "list":
        key = os.environ.get("QBRAID_API_KEY")
        if not key:
            print("set QBRAID_API_KEY first (export QBRAID_API_KEY=...)")
            return 2
        try:
            devs = list_devices(cfg, key)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
            print(f"qBraid API call failed: {e}")
            return 1
        if args.raw:
            print(json.dumps(devs[:2], indent=2)[:3000])   # first devices, to confirm field names
            return 0
        rows = []
        for d in devs:
            # search across every human field so --filter iqm/garnet catches Braket-hosted IQM
            hay = " ".join(str(d.get(k, "")) for k in ("qrn", "vrn", "name", "vendor", "modality"))
            if args.filter and args.filter.lower() not in hay.lower():
                continue
            rows.append((str(d.get("name") or ""), str(d.get("vendor") or ""),
                         str(d.get("deviceType") or ""), str(_dev_status(d) or ""),
                         str(_dev_id(d) or "")))
        print(f"  {'NAME':22} {'VENDOR':10} {'TYPE':10} {'STATUS':12} ID (use in config)")
        for name, vendor, typ, st, ident in sorted(rows):
            print(f"  {name:22} {vendor:10} {typ:10} {st:12} {ident}")
        print(f"{len(rows)} device(s){' matching '+args.filter if args.filter else ''}")
        return 0

    if args.command == "crosscheck":
        res = do_crosscheck(cfg, os.environ.get("QBRAID_API_KEY"))
        if not args.quiet:
            if not res["sources"]:
                print("crosscheck: no native source available "
                      "(need amazon-braket-sdk + AWS creds and/or qiskit-ibm-runtime + IBM_QUANTUM_TOKEN).")
            else:
                print(f"crosscheck: {res['generated']}  sources={','.join(res['sources'])}  "
                      f"checked={res['checked']}  agree={res['agree']}  disagree={res['disagree']}")
                for d in res["devices"]:
                    if not d["agree"]:
                        print(f"  DISAGREE {d['device']}: qBraid={d['qbraid']['status']} "
                              f"vs {d['native']['source']}={d['native']['status']}")
        return 0

    if args.command == "poll":
        res = do_poll(cfg, os.environ.get("QBRAID_API_KEY"))
        report = build_report(cfg)
        path = write_report(cfg, report)
        if not args.quiet:
            st = res.get("statuses", {})
            if st and len(st) <= 8:
                print(f"uptime: {res.get('at','')}  " + "  ".join(f"{k.split(':')[-1]}={v}" for k, v in st.items()))
            elif st:
                from collections import Counter
                c = Counter(st.values())
                print(f"uptime: {res.get('at','')}  {len(st)} devices  " +
                      "  ".join(f"{k}={n}" for k, n in c.most_common()))
            unsched = sum(len(d["unscheduled"]) for d in report["devices"])
            print(f"uptime: report -> {path}  ({len(report['devices'])} devices tracked, "
                  f"{unsched} unscheduled outage(s) on scheduled machines)")
        return 0

    # report
    report = build_report(cfg)
    path = write_report(cfg, report)
    if not args.quiet:
        for d in report["devices"]:
            print(f"  {d['device']:16} now={d['current_status']:10} "
                  f"uptime={d['uptime_pct']}%  unscheduled={len(d['unscheduled'])}")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
