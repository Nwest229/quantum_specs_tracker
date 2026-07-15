"""IBM Quantum adapter.

IBM no longer exposes anonymous calibration JSON; per-backend calibration
comes from ``qiskit-ibm-runtime`` authenticated with an API token
(env ``IBM_QUANTUM_TOKEN`` or config ``token``). When present, this adapter
pulls the *full* per-gate calibration and reads structured fields straight from
IBM's own API objects — never by scraping text:

  * qubit count + coupling-map edge count (topology)
  * processor family/revision -> model, and Quantum Volume if published
  * per-gate error -> 1Q/2Q/SPAM fidelity statistics (max/min/mean/median)
  * per-gate + readout duration -> 1Q/2Q gate times and readout time

Without a token (or without the SDK installed) it is a clean no-op that only
emits a skip warning. Every extracted field carries provenance; anything the API
doesn't provide is simply left blank (never invented).
"""
from __future__ import annotations

import os
import statistics
from typing import Iterable, Optional

from ..models import BackendRecord, F, now_iso
from ..normalize import apply_fidelity_stats
from .base import Adapter

_SRC = "https://quantum.cloud.ibm.com/"
# IBM's real single-qubit gates; rz is virtual (0 error/0 duration) and id is a
# no-op, so both are excluded from physical 1Q statistics.
_ONE_Q_GATES = ("sx", "x")


class IBMAdapter(Adapter):
    vendor = "ibm"
    tier = "calibration-api"

    def fetch(self) -> Iterable[BackendRecord]:
        token = self.config.get("token") or os.environ.get("IBM_QUANTUM_TOKEN")
        if not token:
            return self._skip("No IBM_QUANTUM_TOKEN; skipping IBM calibration pull.")

        try:
            from qiskit_ibm_runtime import QiskitRuntimeService  # type: ignore
        except ImportError:
            return self._skip("qiskit-ibm-runtime not installed.")

        # Any connection/auth/SSL failure must degrade to a skip, never crash the
        # whole build — IBM is an optional enrichment tier.
        try:
            kwargs = {"channel": "ibm_quantum_platform", "token": token}
            instance = self.config.get("instance")   # optional CRN for the new platform
            if instance:
                kwargs["instance"] = instance
            service = QiskitRuntimeService(**kwargs)
            wanted = set(self.config.get("backends", []))
            out = []
            for backend in service.backends(operational=True):
                if wanted and backend.name not in wanted:
                    continue
                try:
                    out.append(self._to_record(backend))
                except Exception as e:  # noqa: BLE001 - skip one bad device, keep the rest
                    self.report_backend_error(backend, e)
            return out
        except Exception as e:  # noqa: BLE001
            return self._skip(f"IBM API call failed ({type(e).__name__}): "
                              f"{str(e)[:200]}")

    def report_backend_error(self, backend, err) -> None:
        # best-effort: note a single device we couldn't parse, without failing the run
        name = getattr(backend, "name", "?")
        self.config.setdefault("_warnings", []).append(f"IBM {name}: {err}")

    def _skip(self, msg: str):
        if not self.config.get("emit_skips"):
            return []
        rec = BackendRecord(backend_name="__ibm_unavailable__", vendor="ibm")
        self.warn(rec, msg)
        rec.meta["skipped"] = True
        return [rec]

    def _to_record(self, backend) -> BackendRecord:
        retrieved = now_iso()
        # Clean the cloud id ("ibm_kingston") to the same display name the CSV
        # importer produces ("IBM Kingston"), so live API rows MERGE with (and,
        # running first, supersede) the historical CSV rows instead of duplicating.
        rec = BackendRecord(backend_name=_clean_name(backend.name), vendor="ibm")
        rec.system_name = backend.name
        src = f"{_SRC} (backend={backend.name})"
        tier = self.tier

        def put(path: str, val, method: str) -> None:
            if val is not None:
                rec.set(path, F(val, src, retrieved, method, tier))

        # ---- topology -------------------------------------------------
        put("qpu_topology.qubits", getattr(backend, "num_qubits", None), "vendor-spec")
        rec.set("qpu_topology.type", F("heavy-hex", src, retrieved, "vendor-spec", tier))
        put("qpu_topology.edges", _edge_count(backend), "vendor-spec")

        # ---- model + quantum volume from the configuration ------------
        conf = _safe(getattr(backend, "configuration", None))
        if conf is not None:
            pt = getattr(conf, "processor_type", None)
            if isinstance(pt, dict) and pt.get("family"):
                rec.model = " ".join(str(x) for x in (pt.get("family"), pt.get("revision")) if x)
            put("quantum_volume", getattr(conf, "quantum_volume", None), "measured")

        # ---- per-gate calibration -------------------------------------
        props = _safe(getattr(backend, "properties", None))
        if props is None:
            self.warn(rec, "no calibration properties available")
            return rec

        two_q_f, one_q_f, two_q_t, one_q_t = [], [], [], []
        for gate in getattr(props, "gates", []):
            qubits = list(getattr(gate, "qubits", []))
            fid = _gate_fidelity(props, gate)
            length = _gate_length(props, gate)
            if len(qubits) == 2:
                if fid is not None:
                    two_q_f.append(fid)
                if length is not None:
                    two_q_t.append(length)
            elif len(qubits) == 1 and getattr(gate, "gate", "") in _ONE_Q_GATES:
                if fid is not None:
                    one_q_f.append(fid)
                if length is not None:
                    one_q_t.append(length)

        spam, ro_t = [], []
        for qi in range(getattr(backend, "num_qubits", 0)):
            try:
                spam.append(1.0 - props.readout_error(qi))
            except Exception:  # noqa: BLE001
                pass
            rl = _call(props, "readout_length", qi)
            if rl is not None:
                ro_t.append(rl)

        apply_fidelity_stats(rec, two_q_f, one_q_f, spam, source=src,
                             retrieved=retrieved, kind=tier)

        # ---- operation speed (median gate/readout durations, seconds) --
        put("operation_speed.2q_gate_time_s", _median(two_q_t), "median")
        put("operation_speed.1q_gate_time_s", _median(one_q_t), "median")
        put("operation_speed.readout_time_s", _median(ro_t), "median")
        return rec


# ---------------------------------------------------------------------------
# small, defensive helpers — any missing/None API surface degrades to blank
# ---------------------------------------------------------------------------
def _clean_name(raw: str) -> str:
    """'ibm_kingston' -> 'IBM Kingston' (matches csv_source._backend_name)."""
    n = (raw or "").strip()
    if "_" in n and n == n.lower():
        n = n.split("_", 1)[1].replace("_", " ").strip().title()
    if not n:
        n = raw
    if "ibm" not in n.lower():
        n = f"IBM {n}"
    return n


def _safe(fn):
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _call(obj, name, *args):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def _edge_count(backend) -> Optional[int]:
    """Number of *undirected* coupling-map edges."""
    cmap = getattr(backend, "coupling_map", None)
    if cmap is None:
        return None
    try:
        pairs = {frozenset((int(a), int(b))) for a, b in cmap if a != b}
    except (TypeError, ValueError):
        return None
    return len(pairs) or None


def _gate_fidelity(props, gate) -> Optional[float]:
    err = _call(props, "gate_error", getattr(gate, "gate", None), getattr(gate, "qubits", None))
    return (1.0 - err) if err is not None else None


def _gate_length(props, gate) -> Optional[float]:
    return _call(props, "gate_length", getattr(gate, "gate", None), getattr(gate, "qubits", None))


def _median(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 12) if vals else None
