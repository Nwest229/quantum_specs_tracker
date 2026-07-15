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
        provider = getattr(dev, "provider_name", "") or "aws-braket"
        rec = BackendRecord(backend_name=dev.name, vendor=provider.lower())
        rec.system_name = dev.name
        rec.meta["braket_arn"] = dev.arn

        props = dev.properties.dict() if hasattr(dev.properties, "dict") else {}
        # qubit count / topology
        paradigm = props.get("paradigm", {})
        qcount = paradigm.get("qubitCount")
        if qcount is not None:
            rec.set("qpu_topology.qubits", F(qcount, _SRC, retrieved, "vendor-spec", self.tier))
        conn = paradigm.get("connectivity", {})
        if conn.get("fullyConnected"):
            rec.set("qpu_topology.type", F("all-to-all", _SRC, retrieved, "vendor-spec", self.tier))

        # standardized fidelities (present for many providers)
        std = (props.get("provider", {}) or {}).get("standardized", {}) \
            or props.get("standardized", {})
        one_q = _dig(std, "oneQubitProperties")
        two_q = _dig(std, "twoQubitProperties")
        f1 = _collect_fidelity(one_q, ("f1Q_simultaneous_RB", "f1Q_RB"))
        f2 = _collect_fidelity(two_q, ("f2Q_simultaneous_RB", "f2Q_RB", "fCZ", "fGST"))
        if f1:
            rec.set("fidelity.1q_avg", F(round(sum(f1) / len(f1), 6), _SRC, retrieved,
                                         "average", self.tier))
        if f2:
            rec.set("fidelity.2q_avg", F(round(sum(f2) / len(f2), 6), _SRC, retrieved,
                                         "average", self.tier))
            rec.derived_metrics.setdefault("fidelity_stats", {})["2q"] = {
                "n": len(f2), "max": max(f2), "min": min(f2),
                "mean": sum(f2) / len(f2),
            }
        return rec


def _dig(d: dict, key: str):
    return d.get(key) if isinstance(d, dict) else None


def _collect_fidelity(block, keys) -> list:
    """Braket stores per-qubit/-pair dicts of {name: {fidelity, ...}}."""
    out = []
    if not isinstance(block, dict):
        return out
    for entry in block.values():
        props = entry.get("fidelity") if isinstance(entry, dict) else None
        if isinstance(props, list):
            for item in props:
                name = item.get("fidelityType", {}).get("name")
                if name in keys and item.get("fidelity") is not None:
                    out.append(float(item["fidelity"]))
    return out
