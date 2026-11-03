# Reckitt Retailer Onboarding POC (Nablon AI)

Retailer files to bronze-ready output, built to the agreed POC architecture: S3 in and S3 out, the whole solution running as one Databricks App. Goal: onboarding from 10 days to 2–3 days.

This is a fresh build. `reckitt-s2b` and `reckitt-s2b - Copy` are untouched; the proven logic from them (discovery, mapping/Veritas JSON, transforms, harmonisation, checks) is ported into `src/onboarding_poc/core/`.

## Modules and where they live

| Module | Code | Screen | Gate |
|---|---|---|---|
| M1 Intake | `modules/m1_intake.py` | Start onboarding, Add files | |
| M2 AI Discovery | `modules/m2_discovery.py` | AI Discovery | |
| M3 AI-assisted mapping | `modules/m3_mapping.py` | Mapping (source ⇄ target canvas) | A: mapping approval |
| M4 Code harmonisation | `modules/m4_code_harmonisation.py` | Harmonisation | B: harmonisation review |
| M5 Value harmonisation | `modules/m5_value_harmonisation.py` | Harmonisation | B |
| M6 Enrich and validate | `modules/m6_enrich_validate.py` | Validate & preview | |
| M7 Output and sign-off | `modules/m7_output_signoff.py` | Sign off | C: sign-off |
| M8 File journey and learning | `modules/m8_journey_learning.py` | File journey (every screen) | |

Shared pieces:
- `service.py` wires the modules together.
- `context.py` holds config, storage, the LLM, the MDM cache and the parsed-file cache.
- `session.py` defines the session state and the trace.
- `storage.py` covers local or S3 storage.
- `config.py` loads the master config.
- `api/app.py` is the REST API and serves the front end.

## How a session flows

1. **Start onboarding.** Pick market and retailer, fill in the business fields (suggested values come from Veritas conventions and market defaults), and optionally upload the mapping file (target schema). Without one, the built-in POS_SELLOUT stand-in is used.
2. **Add files.** Upload them, or give an S3 path with the client's credentials (used once, never stored). Each sheet is fingerprinted. A layout already in the mapping library is marked *known*.
3. **Analyse.** This runs M2 and M3 in one process:
   - Rules profile every column.
   - The LLM gets a sample of 10–20 rows and returns table-, column- and data-level metadata.
   - Mappings are proposed with confidence and a reason for every link.
4. **Gate A.** People review the canvas: accept, change or clear links. A changed link becomes "human_modified" and keeps the reviewer's name. Approval is refused while a mandatory column is unmapped.
5. **Harmonisation.** M4 matches RB codes through the crosswalk, then EAN, then description or address, and keeps the top 5 candidates for each code. M5 shows date, number, suffix, currency and unit rules with before/after examples.
6. **Gate B.** Refused while flagged codes or value rules with issues are undecided. On approval, people's decisions are written to the code crosswalk.
7. **Validate.** Builds the rows, applies MDM enrichment and runs the checks. It also runs the **Veritas compare**: the generated Veritas JSON against the existing Veritas mapping, column by column. This is the POC KPI.
8. **Gate C, sign-off.** Writes the bronze-ready files (physical layout), Veritas JSON, mapping specs, discovery reports, validation, `trace.json` and `manifest.json` to `outputs/<session>/`. The mappings then go to the **mapping library**.
9. **Next file with the same layout.** Discovery and mapping are skipped and Gate A passes automatically. If every code resolves from the crosswalk, Gate B passes too. Sign-off always stays with a person.

**Trace.** Every step writes a trace event recording the source, who decided (`ai:<model>`, `rules`, `library` or a person), why and how confident, when and at which version, and before/after.

## Storage layout (S3 bucket prefix or local folder)

```
inbox/<session>/<file>/...           uploaded or copied retailer files, target/ mapping file
state/sessions/<session>/            session.json, trace.json, discovery/, mappings/, harmonisation/, validation/
library/mappings/<mkt>/<retailer>/   signed-off mappings (Veritas JSON + spec), versioned
library/crosswalk/<entity>/<mkt>/    learned retailer code -> RB code
library/veritas_reference/           existing Veritas exports (for conventions and the compare KPI)
mdm/                                 ERP product master, RB customer master (cached in memory)
outputs/<session>/                   bronze/, mappings/, discovery/, validation/, trace.json, manifest.json
```

## Run locally

```bash
pip install -e ".[dev,llm]"
python -m onboarding_poc seed          # dummy MDM, Veritas #4285, sample files into data/store
python -m onboarding_poc serve 8000    # API on http://localhost:8000/docs
pytest -q
```

## Front end

The front end is React and TypeScript (Vite) in `frontend/`. It is served by the same FastAPI app, at the same URL, as the API.

**Colours.** It uses Nablon colours only: blue is the single accent, and there are no semantic colours. Status is shown with words, outlines and dash patterns.

**Screens** (in `frontend/src/screens/`):
- `Start.tsx`: Start onboarding (business fields, mapping file)
- `Files.tsx`: Add files (upload, S3 path, samples, preview)
- `Discovery.tsx`: AI Discovery (table and column metadata, change a column type, open questions)
- `Mapping.tsx`: Mapping. This is the Veritas-style source ⇄ target canvas:
  - lines show the link state (accepted / human modified / library, AI suggested, needs a decision);
  - a link editor covers column, constant, join, date and expression;
  - tabs show the output preview and the Veritas JSON;
  - Gate A is approved here.
- `Harmonisation.tsx`: codes with the top 5 candidates and MDM search, value rules with before/after examples, Gate B
- `Validate.tsx`: checks, Veritas compare and preview
- `Signoff.tsx`: readiness, Gate C and the output files
- `Journey.tsx`: file journey and decision log (M8), shown on every screen

**Building it.**

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /api to :8000
npm run build    # writes frontend/dist, served by FastAPI (committed so the Databricks App needs no Node build)
```

## Config

The master config is `config/master_config.yaml`, with env overrides in the form `POC_<SECTION>__<KEY>`. It holds:
- the S3 bucket, prefix and region, plus the names of the credential env vars;
- the folder paths;
- the LLM: `provider` (`none` | `openai` | `azure_openai` | `databricks`), `base_url`, `model`, `api_key_env`;
- the discovery sample size;
- the review thresholds;
- the MDM files and cache;
- the auth mode;
- the output format.

With `llm.provider: none` everything runs on rules, so an LLM outage slows review but never stops a session. Runtime in Reckitt stays on OpenAI models unless the Reckitt platform team approves another one.

## Deploy (Databricks App)

`app.yaml` starts uvicorn with `auth.mode = databricks_app` (the user comes from `X-Forwarded-Email`) and `storage.backend = s3`. Map the secret-scope keys `s3-access-key`, `s3-secret-key` and `llm-api-key` as app resources, set `POC_STORAGE__S3__BUCKET`, build `frontend/dist` and deploy from GitHub through CI/CD.

## Not in POC scope

- Writing to Reckitt's catalog or Delta tables.
- Lakeflow jobs.
- Value harmonisation beyond the five rule kinds. Jonathan's second type is still to be confirmed.

## Open items

- Who the end user is (Reckitt or the retailers).
- The POC S3 bucket and IAM on the Stockholm workspace.
- The approved LLM endpoint.
- The real MDM extracts.
- The full target schema and Veritas operator catalogue.
- Veritas exports for the 5 POC retailers.
