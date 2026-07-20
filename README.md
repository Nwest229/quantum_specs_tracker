# Quantum backend tracker

A small pipeline I built to pull together a comparison of commercial quantum
computers — qubits, fidelity, speed, features, pricing — into one JSON file plus
a table I can sort and chart. The rule it follows everywhere: every number has to
carry its source (URL + date). If I can't find a number, it stays
`"Not publicly disclosed"` — it never makes anything up.

The Python side (the `qscrape` package) is mine, but the overall structure is a
fairly standard config-driven scraper pattern I took from online examples, not
something I invented. **The front end — `viewer.html` and all the charts — is
pure Claude. I didn't write any of that.**

## Running it

I run everything through the project's virtualenv, because the system Python on
my Mac ships an old SSL that breaks the IBM API:

```bash
.venv/bin/python -m qscrape                      # build data/backends.json
.venv/bin/python -m qscrape --xlsx               # also write the Excel workbook
.venv/bin/python -m qscrape --only ionq          # just one vendor (handy when testing a new entry)
.venv/bin/python -m unittest tests.test_pipeline # the tests
```

To see the table and charts, serve the folder and open the viewer:

```bash
.venv/bin/python -m http.server
# then open http://localhost:8000/viewer.html
```

What comes out:

- `data/backends.json` — the combined data, one object per backend, every value cited.
- `data/quantum_tracker.xlsx` — the same thing as an Excel file with charts.
- `viewer.html` — the sortable table + charts (Claude's work).

## Where the data actually comes from

Being honest: most of it is entered by hand (always with a source). Roughly:

- **IonQ Forte** is the only page that really scrapes live (a regex on ionq.com).
- **IBM** comes from IBM's own API (`qiskit-ibm-runtime`) when I set a token — real
  per-gate calibration (fidelity, gate times, readout, edges, QV). It needs
  `IBM_QUANTUM_TOKEN` and an instance CRN. Without those, IBM falls back to
  hand-entered values.
- **Everything else** is either a number I read off the vendor's page/PDF and typed
  into `config/sources.json` as a cited `const`, or a row from my market
  spreadsheet `nisqaas_market_prices.csv`.

The reason so much is manual: most vendor pages are JavaScript-rendered, so a
plain fetch just gets an empty shell — the tool can't run a browser, so for those
I read the number myself and cite it. eleQtron's own systems are left out on
purpose; this is a competitor-only view.

## How it's put together

- `qscrape/` — the pipeline. `config/sources.json` holds all the per-vendor rules;
  the code is generic and just executes them.
- Each value is stored with its provenance (value + source + date + method), and
  the schema in `schema/backend.schema.json` is checked on every run.
- When two sources describe the same backend, the higher-priority one wins
  (API > vendor page > my CSV), so live data overrides the hand-entered stuff.
- `data/run_report.json` lists warnings and any validation problems from the last run.

## Adding a new backend

Two ways.

### Option 1 — by hand

Add an entry to `spec_sources` in `config/sources.json`:

```json
{
  "vendor": "iqm",
  "tier": "vendor",
  "backend_name": "IQM Garnet",
  "meta": { "system_name": "Garnet", "type": "gate-based (superconducting)" },
  "url": "https://www.iqmacademy.com/...",
  "fields": {
    "qpu_topology.qubits": { "const": 20, "as": "int", "method": "vendor-spec", "source": "https://..." },
    "fidelity.2q_avg":     { "const": 0.995, "as": "float", "method": "average", "source": "https://..." }
  }
}
```

Field rules: `regex` / `selector` (CSS) / `json_path` pull a value from the page;
`const` is a value I type in myself, paired with a `source`. `as` converts it:
`int`, `float`, `fraction` (99.7% → 0.997), `fraction_complement` (error 0.4% →
0.996), `str`.

### Option 2 — let AI read the page for me

For the JS pages I can't scrape, I paste the prompt below into ChatGPT (with
browsing on) and give it the vendor link. It reads the page and hands back a
config block I paste straight into `spec_sources`.

**The prompt:**

```
You are a data-extraction assistant for a provenance-first quantum-computing hardware database. Browse the page at:

PASTE URL HERE

Extract the hardware specs for the quantum backend described there and return them as ONE JSON object that matches the exact template and rules below. This will be pasted directly into a config file, so output ONLY the JSON — no prose, no markdown fences.

HARD RULES (critical):
1. NEVER invent or guess. If a value is not explicitly stated on the page, OMIT that field entirely. Do not estimate.
2. Every extracted field must include a "source" = the exact URL where you saw it (the page above, or a more specific sub-page/PDF if that's where the number actually is).
3. Convert units EXACTLY as specified below. When unsure how to convert, omit the field rather than risk a wrong value.
4. Fidelities: output as a DECIMAL FRACTION between 0 and 1 (99.7% -> 0.997), with "as":"float". If the page gives an ERROR rate instead of a fidelity, still convert to fidelity (error 0.5% -> 0.995).
5. Gate/readout times: output in SECONDS as a float (20 ns -> 2e-8), "as":"float".
6. Prices: output as a string "USD <amount>" or "EUR <amount>" (e.g. "USD 0.30"). Per-gate/shot/task prices are usually USD; large per-system/per-month figures are often EUR — use whatever currency the page states.
7. "method" must be one of: measured, average, median, minimum, maximum, vendor-spec, benchmark-derived, theoretical, publication.

FIELD REFERENCE (path -> meaning, coercion "as"):
- meta.model, meta.system_name, meta.commercial_release  (plain strings, no source)
- meta.type  -> one of: gate-based (superconducting) | gate-based (trapped-ion) | analog (neutral-atom) | quantum annealer | ...
- qpu_topology.qubits (int) ; qpu_topology.edges (int) ; qpu_topology.type (str, e.g. "heavy-hex", "all-to-all", "square lattice")
- fidelity.2q_max / 2q_avg / 2q_median / 2q_min (float 0-1)
- fidelity.1q_max / 1q_avg / 1q_min (float 0-1) ; fidelity.spam_avg (float 0-1)
- operation_speed.1q_gate_time_s / 2q_gate_time_s / readout_time_s (float, seconds)
- operation_speed.shot_rate_min / shot_rate_avg / shot_rate_max (float, Hz) ; operation_speed.clops (float, Hz)
- quantum_volume (int) ; black_box (int = Algorithmic Qubits) ; vendor_metric (str = the vendor's headline figure) ; argmax (str)
- features.mid_circuit_measurement / conditional_logic / parallel_2q / qubit_reuse / hybrid_execution / uptime  (values like "yes","no", a number, or "99%")
- pricing.per_1q_gate / per_2q_gate / per_iteration / per_shot / per_task / per_second / per_hour / per_month / per_system  (strings, "USD x"/"EUR x")
- pricing.comments (str)

OUTPUT TEMPLATE (fill it; delete any field not found on the page):
{
  "vendor": "<lowercase vendor key, e.g. ibm, ionq, quantinuum>",
  "tier": "vendor",
  "backend_name": "<Vendor ProductName, e.g. IBM Heron r2>",
  "meta": { "model": "", "system_name": "", "commercial_release": "", "type": "" },
  "url": "<the page URL>",
  "_status": "Extracted by ChatGPT from <url> on <today's date>; verify before trusting.",
  "fields": {
    "qpu_topology.qubits": { "const": 0, "as": "int", "method": "vendor-spec", "source": "<url>" },
    "fidelity.2q_avg":     { "const": 0.0, "as": "float", "method": "average", "source": "<url>" },
    "quantum_volume":      { "const": 0, "as": "int", "method": "measured", "source": "<url>" },
    "pricing.per_shot":    { "const": "USD 0.00", "method": "vendor-spec", "source": "<url>" }
  }
}

Include ONLY the fields you actually found. If you found nothing beyond the qubit count, return just that one field. Return the JSON object and nothing else.
```

**How to use it:**

1. Paste the prompt into ChatGPT (browsing on) and replace `<PASTE URL HERE>` with the vendor page link.
2. It returns a JSON object.
3. Paste that object into the `spec_sources` array in `config/sources.json` (comma-separate it from the others).
4. Test just that vendor: `.venv/bin/python -m qscrape --only <vendor>`, then do a full run.
5. Spot-check the numbers — browsing models sometimes misread tables, so treat each entry as unverified until I've eyeballed it (that's what the `_status: ...verify` line is for).

## A few things worth knowing

- **No time series.** My price data was collected on random dates, not a regular
  schedule, so the charts are a current snapshot, not a trend over time.
- **`theoretical_max`** (`2^min(N, 1/error)`) is a rough headroom number — I keep
  it but trust measured Quantum Volume more.
- **Re-running is safe.** It rebuilds from the sources, and the JSON diffs cleanly
  so I can see what actually changed between runs.
