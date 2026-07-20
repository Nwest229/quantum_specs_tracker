"""Amazon Braket adapter -- the richest *structured* multi-vendor source.

Braket's ``get_device`` returns a JSON capabilities document (the
``standardized`` block of provider properties) that includes one- and
two-qubit fidelities, timings and connectivity for IonQ, Rigetti, IQM and
QuEra devices. This adapter uses the ``amazon-braket-sdk`` when it and AWS
credentials are present; otherwise it is a no-op that explains what's missing.

No values are invented: everything comes from the live device document.
"""
from __future__ import annotations

from typing import Iterable

from ..models import BackendRecord, F, now_iso
from .base import Adapter

_SRC = "https://docs.aws.amazon.com/braket/latest/developerguide/braket-devices.html"


class BraketAdapter(Adapter):
    vendor = "aws-braket"
    tier = "cloud-api"

    def fetch(self) -> Iterable[BackendRecord]:
        try:
            from braket.aws import AwsDevice  # type: ignore
        except ImportError:
            rec = BackendRecord(backend_name="__braket_unavailable__", vendor="aws-braket")
            self.warn(rec, "amazon-braket-sdk not installed; skipping. "
                           "pip install amazon-braket-sdk and configure AWS creds.")
            rec.meta["skipped"] = True
            return [rec] if self.config.get("emit_skips") else []

        arns = self.config.get("device_arns")
        try:
            devices = ([AwsDevice(a) for a in arns] if arns
                       else AwsDevice.get_devices(types=["QPU"]))
        except Exception as e:  # noqa: BLE001 - AWS raises many creds/network errors
            rec = BackendRecord(backend_name="__braket_error__", vendor="aws-braket")
            self.warn(rec, f"Braket device listing failed: {e}")
            rec.meta["skipped"] = True
            return [rec] if self.config.get("emit_skips") else []

        out = []
        for dev in devices:
            try:
                out.append(self._to_record(dev))
            except Exception as e:  # noqa: BLE001
                rec = BackendRecord(backend_name=getattr(dev, "name", "?"),
                                    vendor="aws-braket")
                self.warn(rec, f"parse failed: {e}")
                out.append(rec)
        return out

    def _to_record(self, dev) -> BackendRecord:
        retrieved = now_iso()
        provider = getattr(dev, "provider_name", "") or ""
        # Prefix the provider so Braket's bare device name ("Garnet") becomes the
        # tracker's name ("IQM Garnet") and MERGES with the existing row instead
        # of duplicating it. Genuinely new devices (Cepheus, IBEX) stay separate.
        name = dev.name
        if provider and provider.lower() not in name.lower():
            name = f"{provider} {name}"
        rec = BackendRecord(backend_name=name, vendor=(provider or "aws-braket").lower())
        rec.system_name = dev.name
        rec.meta["braket_arn"] = dev.arn

        try:
            props = dev.properties.dict() if hasattr(dev.properties, "dict") else {}
        except Exception:  # noqa: BLE001 - some providers (IonQ) mangle .dict() keys
            props = {}
        # qubit count / topology
        paradigm = props.get("paradigm", {}) or {}
        qcount = paradigm.get("qubitCount")
        if qcount is not None:
            rec.set("qpu_topology.qubits", F(qcount, _SRC, retrieved, "vendor-spec", self.tier))
        conn = paradigm.get("connectivity", {}) or {}
        if conn.get("fullyConnected"):
            rec.set("qpu_topology.type", F("all-to-all", _SRC, retrieved, "vendor-spec", self.tier))

        self._extract_calibration(rec, props.get("provider", {}) or {}, retrieved)
        return rec

    def _extract_calibration(self, rec, provider: dict, retrieved: str) -> None:
        """Pull fidelity/edges from the (provider-specific) capabilities block.

        Each vendor nests it differently, e.g. IQM under provider.properties.
        one_qubit[q].f1Q_simultaneous_RB / .fRO, Rigetti under provider.specs
        (architecture.edges + per-qubit specs). We look up known key names and
        never invent: an unrecognised layout simply yields nothing.
        """
        if not isinstance(provider, dict):
            return

        def put(path, val, method):
            if val is not None:
                rec.set(path, F(val, _SRC, retrieved, method, self.tier))

        # IonQ: aggregate means under provider.fidelity, gate times under provider.timing
        fid = provider.get("fidelity")
        if isinstance(fid, dict):
            put("fidelity.1q_avg", _mean(fid.get("1Q")), "average")
            put("fidelity.2q_avg", _mean(fid.get("2Q")), "average")
            put("fidelity.spam_avg", _mean(fid.get("spam")), "average")
        timing = provider.get("timing")
        if isinstance(timing, dict):
            put("operation_speed.1q_gate_time_s", _num(timing.get("1Q")), "vendor-spec")
            put("operation_speed.2q_gate_time_s", _num(timing.get("2Q")), "vendor-spec")
            put("operation_speed.readout_time_s", _num(timing.get("readout")), "vendor-spec")

        specs = provider.get("specs") if isinstance(provider.get("specs"), dict) else {}
        # Rigetti exposes the coupling map as specs.architecture.edges
        arch = specs.get("architecture") if isinstance(specs, dict) else None
        if isinstance(arch, dict) and isinstance(arch.get("edges"), list) and arch["edges"]:
            put("qpu_topology.edges", len(arch["edges"]), "vendor-spec")

        # per-qubit / per-pair fidelity: IQM (properties.one_qubit/two_qubit)
        # plus Rigetti (specs.benchmarks). Each vendor uses its own layout; an
        # unrecognised layout simply contributes nothing (never invented).
        inner = provider.get("properties") if isinstance(provider.get("properties"), dict) else {}
        one = _block(inner, ("one_qubit", "1Q", "oneQubitProperties"))
        two = _block(inner, ("two_qubit", "2Q", "twoQubitProperties"))
        f1 = _vals(one, ("f1Q_simultaneous_RB", "f1Q_RB", "f1QRB", "fRB"))
        spam = _vals(one, ("fRO",))
        f2 = _vals(two, ("f2Q_simultaneous_RB", "fCZ", "f2Q_RB", "fXY", "fGST"))
        r1, r2, rspam = _rigetti_bench(specs)
        f1 += r1
        f2 += r2
        spam += rspam

        def stats(prefix, vals):
            if not vals:
                return
            put(f"fidelity.{prefix}_avg", round(sum(vals) / len(vals), 6), "average")
            put(f"fidelity.{prefix}_max", round(max(vals), 6), "maximum")
            put(f"fidelity.{prefix}_min", round(min(vals), 6), "minimum")
        stats("1q", f1)
        stats("2q", f2)
        if spam:
            put("fidelity.spam_avg", round(sum(spam) / len(spam), 6), "average")


def _rigetti_bench(specs) -> tuple:
    """Rigetti specs.benchmarks -> (1Q, 2Q, SPAM) fidelity lists.

    Each benchmark carries node_count (1 or 2) and per-site characteristics like
    {'name': 'fRB'/'fCZ'/'fRO', 'value': 0.996}; node_count buckets 1Q vs 2Q.
    """
    f1, f2, spam = [], [], []
    for bench in (specs.get("benchmarks") or []):
        if not isinstance(bench, dict):
            continue
        nc = bench.get("node_count")
        for site in (bench.get("sites") or []):
            for ch in (site.get("characteristics") or []):
                name, val = ch.get("name"), ch.get("value")
                if not isinstance(val, (int, float)):
                    continue
                if name in ("fRO", "fActiveReset"):
                    spam.append(float(val))
                elif nc == 1 and name in ("fRB", "f1QRB", "fSim"):
                    f1.append(float(val))
                elif nc == 2 and name in ("fCZ", "fXY", "fISWAP", "f2QRB", "fRB"):
                    f2.append(float(val))
    return f1, f2, spam


def _mean(block):
    """IonQ-style aggregate: {'mean': 0.9998} -> 0.9998."""
    if isinstance(block, dict) and isinstance(block.get("mean"), (int, float)):
        return round(float(block["mean"]), 6)
    return None


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _block(inner, keys) -> dict:
    """First non-empty dict among candidate key names."""
    if not isinstance(inner, dict):
        return {}
    for k in keys:
        v = inner.get(k)
        if isinstance(v, dict) and v:
            return v
    return {}


def _vals(block, keys) -> list:
    """Collect the first matching numeric fidelity from each per-qubit/-pair entry."""
    out = []
    if not isinstance(block, dict):
        return out
    for entry in block.values():
        if not isinstance(entry, dict):
            continue
        for k in keys:
            v = entry.get(k)
            if isinstance(v, (int, float)):
                out.append(float(v))
                break
    return out
