"""Adapter registry.

Maps a source-tier name (used in config) to an Adapter class. HtmlSpecAdapter
is instantiated per-backend entry; API adapters are instantiated once and may
emit many records.
"""
from .base import Adapter
from .braket import BraketAdapter
from .html_spec import HtmlSpecAdapter
from .ibm import IBMAdapter

# adapters that are configured with a single global dict and yield many records
API_ADAPTERS = {
    "ibm": IBMAdapter,
    "braket": BraketAdapter,
}

# per-backend declarative adapter
SPEC_ADAPTER = HtmlSpecAdapter

__all__ = ["Adapter", "API_ADAPTERS", "SPEC_ADAPTER",
           "BraketAdapter", "HtmlSpecAdapter", "IBMAdapter"]
