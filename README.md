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

## Quickstart (uv)

The project is managed with [uv](https://docs.astral.sh/uv/) and lives under
`src/qscrape/` (a standard `src/` layout). Python 3.12 or 3.13.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv yet
uv sync --group dev                               # install runtime + dev deps into .venv

uv run python -m qscrape                          # build data/backends.json
uv run python -m qscrape --xlsx                   # also write the Excel workbook
uv run python -m qscrape --only ionq              # just one vendor (handy when testing a new entry)
uv run pytest                                     # the test suite (25 tests)
```

Or just run `./bootstrap.sh`, which installs uv if needed, syncs dependencies,
installs the pre-commit hooks, and runs lint/typecheck/tests once so you know
the checkout is healthy.

Optional extras (`qiskit-ibm-runtime`, `amazon-braket-sdk`, `beautifulsoup4`,
`openpyxl`) are declared under `[project.optional-dependencies]` in
`pyproject.toml` -- install with e.g. `uv sync --extra ibm --extra braket` or
`uv sync --all-extras --group dev` if you want everything.

To see the table and charts, serve the folder and open the viewer:

```bash
uv run python -m http.server
# then open http://localhost:8000/viewer.html
```

What comes out:

- `data/backends.json` -- the combined data, one object per backend, every value cited.
- `data/quantum_tracker.xlsx` -- the same thing as an Excel file with charts.
- `viewer.html` -- the sortable table + charts (Claude's work).

## Configuration

Runtime settings are centralized in `src/qscrape/settings.py` (pydantic-settings).
Everything is overridable via environment variable or a `.env` file in the repo
root; copy `.env.example` to `.env` to get started. CLI flags (`--config`,
`--out`, `--xlsx`), when passed explicitly, always win over env/settings
defaults.

| Variable                   | Default                     | Meaning                                                                     |
|-----------------------------|------------------------------|-------------------------------------------------------------------------------|
| `QSCRAPE_LOG_LEVEL`         | `INFO`                       | stdlib/structlog log level                                                    |
| `QSCRAPE_LOG_JSON`          | `true`                       | JSON logs when true, human-readable console renderer when false               |
| `QSCRAPE_CONFIG_PATH`       | `config/sources.json`        | source registry read by the pipeline                                          |
| `QSCRAPE_OUT_PATH`          | `data/backends.json`         | combined JSON output path                                                     |
| `QSCRAPE_XLSX_PATH`         | `data/quantum_tracker.xlsx`  | Excel workbook path (used when `--xlsx` has no explicit path)                 |
| `QSCRAPE_CACHE_DIR`         | `.cache`                     | on-disk HTTP cache directory                                                   |
| `QSCRAPE_CACHE_MAX_AGE`     | `86400` (seconds)            | cache freshness window; `--no-cache` forces `0` regardless of this value      |
| `QSCRAPE_REQUEST_DELAY`     | `1.0` (seconds)              | politeness delay between live HTTP fetches                                    |
| `QSCRAPE_IBM_QUANTUM_TOKEN` | unset                        | IBM Quantum Runtime API token (also accepts unprefixed `IBM_QUANTUM_TOKEN`)   |
| `IBM_QUANTUM_TOKEN`         | unset                        | legacy/unprefixed alias for the token above                                   |
| `AWS_ACCESS_KEY_ID`         | unset                        | resolved by boto3's standard credential chain, not read directly by qscrape   |
| `AWS_SECRET_ACCESS_KEY`     | unset                        | see above                                                                      |
| `AWS_REGION`                | unset                        | see above                                                                      |

Per-source `cache_max_age` / `request_delay` overrides in `config/sources.json`
still take precedence over the `QSCRAPE_CACHE_MAX_AGE` / `QSCRAPE_REQUEST_DELAY`
settings-level defaults -- unchanged behavior, just a different fallback layer.

## Tooling rationale

| Tool                | Why                                                                                            |
|----------------------|--------------------------------------------------------------------------------------------------|
| `uv`                 | single fast tool for venv + dependency resolution + lockfile (`uv.lock`); replaces pip/venv       |
| `ruff`               | lint + format in one binary; replaces flake8/isort/black                                          |
| `mypy --strict`      | catches type errors in the provenance/normalization logic before they hit `data/backends.json`    |
| `pytest`             | fixtures + `--cov`, clearer failure output than `unittest`                                        |
| `nox`                | reproducible lint/typecheck/test sessions across Python 3.12 and 3.13, locally and in CI           |
| `pre-commit`         | catches lint/format issues before they reach CI                                                   |
| `pydantic-settings`  | one typed, testable settings object instead of scattered `os.environ.get(...)` calls              |
| `structlog`          | structured (JSON or console) logs for internal diagnostics, without touching the CLI's plain-text run summary |

## Running the checks

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy src               # strict type checking
uv run pytest                 # tests + coverage

# or, via nox (what CI runs):
uv run nox -s lint typecheck tests
```

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

- `src/qscrape/` — the pipeline (standard `src/` layout). `config/sources.json`
  holds all the per-vendor rules; the code is generic and just executes them.
- `tests/unit/` — the pytest suite, one file per module under test, with shared
  fixtures (e.g. `fake_http_factory`) in `tests/conftest.py`. Run with
  `uv run pytest` or `uv run nox -s tests`.
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
