"""Data model for a quantum backend record.

The public output contract is the JSON shape in ``schema/backend.schema.json``.
Internally every leaf value is a :class:`Field` so provenance is never lost.
``BackendRecord.to_dict()`` serialises to the schema shape and, as a side
effect, populates the flat ``sources`` array from each field's provenance.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any, Optional

UNKNOWN = "Not publicly disclosed"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Field:
    """A single value plus where it came from.

    ``value`` may be ``UNKNOWN``; in that case no source entry is emitted.
    ``method`` is the statistic kind (measured/average/median/...).
    ``kind`` is the source tier (vendor/calibration-api/publication/...).
    """

    value: Any = UNKNOWN
    source: str = ""
    retrieved: str = ""
    method: str = ""
    kind: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None and self.value != UNKNOWN

    def as_prov(self) -> dict:
        """Serialise as a {value, source, retrieved, method, kind} object."""
        out: dict[str, Any] = {"value": self.value if self.value is not None else UNKNOWN}
        if self.source:
            out["source"] = self.source
        if self.retrieved:
            out["retrieved"] = self.retrieved
        if self.method:
            out["method"] = self.method
        if self.kind:
            out["kind"] = self.kind
        return out


def F(value: Any = UNKNOWN, source: str = "", retrieved: str = "",
      method: str = "", kind: str = "") -> Field:
    """Terse constructor used by adapters."""
    return Field(value=value, source=source, retrieved=retrieved, method=method, kind=kind)


# Groups of leaf fields, mirroring the schema. Values are Field instances.
_GROUPS = {
    "quantum_volume": None,           # single provValue
    "black_box": None,
    "argmax": None,
    "vendor_metric": None,
    "theoretical_max": None,
    "qpu_topology": ("qubits", "type", "edges"),
    "fidelity": ("2q_max", "2q_avg", "2q_median", "2q_min",
                 "1q_max", "1q_avg", "1q_min", "spam_avg"),
    "operation_speed": ("1q_gate_time_s", "2q_gate_time_s", "readout_time_s",
                        "shot_rate_min", "shot_rate_avg", "shot_rate_max",
                        "clops", "credits_per_hour"),
    "features": ("mid_circuit_measurement", "conditional_logic", "parallel_2q",
                 "qubit_reuse", "hybrid_execution", "uptime"),
    "pricing": ("per_1q_gate", "per_2q_gate", "per_iteration", "per_shot",
                "per_task", "per_second", "per_hour", "per_month",
                "per_system", "comments"),
}

_SCALAR_META = ("id", "type", "model", "system_name",
                "planned_release", "commercial_release")


def slug(*parts: str) -> str:
    """Deterministic id slug from arbitrary text parts."""
    import re as _re
    raw = "-".join(str(p) for p in parts if p)
    return _re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


@dataclass
class BackendRecord:
    backend_name: str
    vendor: str
    # top-level scalar metadata (plain strings, no provenance object)
    id: str = ""       # stable identifier; derived from vendor+backend if unset
    type: str = ""     # system class, e.g. "gate-based (trapped-ion)", "annealer"
    model: str = ""
    system_name: str = ""
    planned_release: str = ""
    commercial_release: str = ""

    # provValue singletons
    quantum_volume: Field = dc_field(default_factory=Field)
    black_box: Field = dc_field(default_factory=Field)
    argmax: Field = dc_field(default_factory=Field)
    vendor_metric: Field = dc_field(default_factory=Field)
    theoretical_max: Field = dc_field(default_factory=Field)

    # grouped Field maps
    qpu_topology: dict[str, Field] = dc_field(default_factory=dict)
    fidelity: dict[str, Field] = dc_field(default_factory=dict)
    operation_speed: dict[str, Field] = dc_field(default_factory=dict)
    features: dict[str, Field] = dc_field(default_factory=dict)
    pricing: dict[str, Field] = dc_field(default_factory=dict)

    derived_metrics: dict[str, Any] = dc_field(default_factory=dict)
    meta: dict[str, Any] = dc_field(default_factory=dict)

    # -- helpers ----------------------------------------------------------
    def set(self, dotted: str, fld: Field) -> None:
        """Set a leaf by dotted path, e.g. record.set('fidelity.2q_avg', F(...))."""
        if "." not in dotted:
            setattr(self, dotted, fld)
            return
        group, leaf = dotted.split(".", 1)
        getattr(self, group)[leaf] = fld

    def _emit_source(self, sources: list, path: str, fld: Optional[Field]) -> None:
        if fld and fld.known and fld.source:
            sources.append({
                "field": path,
                "url": fld.source,
                "retrieved": fld.retrieved or now_iso(),
                "kind": fld.kind or "",
            })

    def to_dict(self) -> dict:
        sources: list[dict] = []
        out: dict[str, Any] = {
            "id": self.id or slug(self.vendor, self.backend_name),
            "backend_name": self.backend_name,
            "vendor": self.vendor,
            "type": self.type or UNKNOWN,
            "model": self.model or UNKNOWN,
            "system_name": self.system_name or UNKNOWN,
            "planned_release": self.planned_release or UNKNOWN,
            "commercial_release": self.commercial_release or UNKNOWN,
        }

        for name in ("quantum_volume", "black_box", "argmax", "vendor_metric", "theoretical_max"):
            fld: Field = getattr(self, name)
            out[name] = fld.as_prov()
            self._emit_source(sources, name, fld)

        for group, leaves in _GROUPS.items():
            if leaves is None:
                continue
            gmap: dict[str, Field] = getattr(self, group)
            obj: dict[str, Any] = {}
            for leaf in leaves:
                fld = gmap.get(leaf, Field())
                obj[leaf] = fld.value if fld.value is not None else UNKNOWN
                self._emit_source(sources, f"{group}.{leaf}", fld)
            out[group] = obj

        if self.derived_metrics:
            out["derived_metrics"] = self.derived_metrics

        # Merge any explicitly attached sources (e.g. from PDFs) then dedupe.
        sources.extend(self.meta.pop("_extra_sources", []))
        out["sources"] = _dedupe_sources(sources)
        out["_meta"] = {"generated": now_iso(), **self.meta}
        return out


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for s in sources:
        key = (s.get("field"), s.get("url"))
        if key in seen:
            continue
        seen.add(key)
        result.append(s)
    return result
