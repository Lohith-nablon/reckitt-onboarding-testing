# Reckitt Retailer Onboarding POC (Nablon AI)

Retailer files to harmonised silver data, built to the POC architecture agreed on 3 Nov: ADLS and Unity Catalog on Azure, one Databricks App in two phases, Reckitt's pipeline in between. Goal: onboarding from 10 days to 2–3 days.

This is a fresh build. `reckitt-s2b` and `reckitt-s2b - Copy` are untouched; the proven logic from them (discovery, mapping/Veritas JSON, transforms, harmonisation, checks) is ported into `src/onboarding_poc/core/`.

## Architecture (after the 3 Nov call)

The solution runs on Azure as one Databricks App, in two phases, with Reckitt's own pipeline in between.

**Phase 1 · Mapping**
1. The raw retailer file is read from the ADLS bronze landing zone, or uploaded in the app.
2. M2 AI Discovery runs, then M3 mapping. A person approves at Gate A.
3. The approved mapping JSON (Veritas format) and the Databricks SQL generated from it are written to ADLS.

**Hand-off · Reckitt's pipeline (manual in the POC)**
4. Reckitt runs the SQL in its existing Databricks pipeline. It loads the bronze tables in Unity Catalog.
5. The app checks Unity Catalog for the rows, and a person confirms the load.

**Phase 2 · Harmonisation (on the loaded tables)**
6. The loaded bronze rows are read from Unity Catalog through a SQL warehouse.
7. M4 matches RB codes and M5 applies value standards. A person approves at Gate B.
8. M6 enriches and validates, and M7 signs off at Gate C.
9. Sign-off writes to silver in Unity Catalog: the harmonised rows and the code crosswalk.

**Learning.** The mapping library and the crosswalk are what M8 learns from. The next file with the same layout skips Discovery and Mapping.

## Modules and where they live

| Module | Code | Screen | Gate |
|---|---|---|---|
| M1 Intake | `modules/m1_intake.py` | Start onboarding, Add files | |
| M2 AI Discovery | `modules/m2_discovery.py` | AI Discovery | |
| M3 AI-assisted mapping, publish JSON + SQL | `modules/m3_mapping.py`, `core/mapping/sqlgen.py` | Mapping (canvas, Conversions, Veritas JSON) | A |
| Pipeline hand-off | `modules/pipeline_handoff.py` | Pipeline | load confirmed |
| M4 Code harmonisation | `modules/m4_code_harmonisation.py` | Harmonisation · Codes | B |
| M5 Conversions (Phase 1), value standards (Phase 2) | `modules/m5_value_harmonisation.py` | Mapping · Conversions; Harmonisation · Value standards | A, B |
| M6 Enrich and validate | `modules/m6_enrich_validate.py` | Validate & preview | |
| M7 Output and sign-off (silver) | `modules/m7_output_signoff.py` | Sign off | C |
| M8 File journey and learning | `modules/m8_journey_learning.py` | File journey (every screen) | |

**Shared pieces:**
- `storage.py`: local, ADLS or S3 storage.
- `warehouse.py`: Unity Catalog through a SQL warehouse, or the local DuckDB stand-in.
- `config.py`: the master config.
- `session.py`: state and trace.
- `service.py`: wiring.
- `api/app.py`: the REST API, which also serves the screens.

## The generated SQL

There is one script per (file, bronze table), written to `outputs/<session>/mapping/<table>__<file>.sql`:

```sql
DELETE FROM <bronze table> WHERE SUBMISSION_ID = '<id>';          -- re-runnable
INSERT INTO <bronze table> (<physical columns in order>)
SELECT ... load columns ... FROM (
  SELECT <one expression per target column>
  FROM read_files('abfss://...raw file...', format => 'csv', header => true, sep => ';')
) AS mapped;
```

**How it behaves:**
- It uses the same trimming, number, date and concatenation rules as the preview, and `try_*` functions so a bad value becomes NULL instead of failing the run.
- Dimension tables keep one row per key.
- RB codes and master attributes are left NULL in bronze; they are filled in silver in Phase 2.

**Source mode.** Set `sql.source_mode: table` if the pipeline lands raw files in a landing table first.

**Test.** `tests/test_sqlgen.py` runs the SQL, transpiled to DuckDB, and checks that it equals the Python preview column by column.

## Storage and tables

```
ADLS  <container>/<prefix>/
  landing/<market>/<retailer>/...       raw retailer files (bronze landing zone)
  inbox/<session>/...                   copies of the files a session works on
  outputs/<session>/mapping/            *.sql, *.veritas.json (fallback copy), manifest.json (Phase 1 output)
  mapping_store/                        approved mapping JSON in Delta format, one row per mapping
  outputs/<session>/                    discovery/, validation/, trace.json, manifest.json   (after sign-off)
  state/sessions/<session>/             session.json, trace.json, discovery/, mappings/, harmonisation/, validation/
  library/mappings/, library/crosswalk/ learning (M8)
  library/veritas_reference/            existing Veritas exports, for conventions and the compare KPI
  mdm/                                  product and customer master extracts

Unity Catalog
  <bronze_catalog>.<schema>.pos_sellout, retailer_product, ...       loaded by Reckitt's pipeline from our SQL
  <silver_catalog>.<schema>.<table>_harmonised                       written at sign-off
  <silver_catalog>.<schema>.retailer_code_crosswalk                  code decisions, per session
```

## Run locally (no Azure needed)

The local mode uses a folder for ADLS and DuckDB files for Unity Catalog, so the whole flow runs on a laptop.

```bash
pip install -e ".[dev,llm]"
python -m onboarding_poc seed          # dummy MDM, Veritas #4285, retailer files in the landing zone
python -m onboarding_poc demo          # optional: one onboarding end to end, no UI
python -m onboarding_poc serve 8000    # http://localhost:8000
pytest -q
```

**The Pipeline screen.** It has a **Run pipeline locally (dev stand-in)** button that plays Reckitt's role on DuckDB. On Databricks this button is hidden unless `warehouse.allow_pipeline_run` is true, because the POC rule is that the app never runs the pipeline.

## Front end

The front end is React and TypeScript (Vite) in `frontend/`. It is served by the same FastAPI app, at the same URL, as the API.

**Colours.** It uses Nablon colours only: blue is the single accent, and there are no semantic colours. Status is shown with words, outlines and dash patterns.

**Screens** (in `frontend/src/screens/`):
- `Start.tsx`: Start onboarding (business fields, mapping file)
- `Files.tsx`: Add files (pick from the ADLS landing zone or upload; preview)
- `Discovery.tsx`: AI Discovery (table and column metadata, change a column type, open questions)
- `Mapping.tsx`: Mapping. This is the Veritas-style source ⇄ target canvas:
  - lines show the link state (accepted / human modified / library, AI suggested, needs a decision);
  - a link editor covers column, constant, join, date and expression;
  - tabs show the Conversions (dates, numbers, suffixes), the output preview and the Veritas JSON;
  - Gate A is approved here, which publishes the mapping JSON and SQL.
- `Pipeline.tsx`: the SQL files, the bronze tables they load, the rows found in Unity Catalog, and the load confirmation
- `Harmonisation.tsx`: on the loaded tables. RB codes with the top 5 candidates and MDM search, value standards for silver, Gate B
- `Validate.tsx`: checks on the silver rows, Veritas compare and preview
- `Signoff.tsx`: readiness, Gate C, the silver tables written and the ADLS files
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
- storage: `local` or `adls` (account, container, prefix, and the env var holding the key or SAS);
- the SQL warehouse (host, HTTP path, token env var) and the Unity Catalog names for bronze, silver and the crosswalk;
- how the generated SQL reads the raw file (`read_files` or a landing table);
- the folder paths;
- the LLM: `provider` (`none` | `openai` | `azure_openai` | `databricks`), `base_url`, `model`, `api_key_env`;
- the discovery sample size;
- the review thresholds;
- the MDM files and cache;
- the auth mode.

With `llm.provider: none` everything runs on rules, so an LLM outage slows review but never stops a session. Runtime in Reckitt stays on OpenAI models unless the Reckitt platform team approves another one.

## Simulate on Nablon's Databricks workspace (Databricks App)

The root `app.yaml` runs the whole POC on a Databricks workspace, with stand-ins for Reckitt's environment:

| Reckitt (after the SOW) | Simulation on Nablon Databricks |
|---|---|
| ADLS landing zone and app folders | UC volume `nablon_poc.onboarding.files` |
| Bronze and silver in Unity Catalog | schemas `nablon_poc.bronze` and `nablon_poc.silver` |
| Mapping JSON stored as Delta in ADLS | Delta table `nablon_poc.bronze.onboarding_mapping_store` |
| Reckitt runs the pipeline on the generated SQL | the app runs that SQL on its SQL warehouse (Pipeline screen) |

**Steps:**

1. Push the repo to GitHub. In Databricks, go to **Workspace → Create → Git folder** and paste the repo URL. For a private repo, first link GitHub under **Settings → Linked accounts**.
2. Open the SQL editor, choose a SQL warehouse, and run the first block of `deploy/databricks/setup.sql`. It creates the catalog, the schemas and the volume.
3. Go to **Compute → Apps → Create app → Custom**. Name it `retailer-onboarding-poc` and add the resource **SQL warehouse**, with key `sql-warehouse` and permission *Can use*.
4. Copy the app's service principal from its **Authorization** tab, then run the GRANT block of `setup.sql` for it.
5. Click **Deploy**, with the source code path set to the Git folder. Open the app URL, then on Overview click **Load demo data** and run a file through the screens.
6. After `git pull` in the Git folder, deploy again to pick up changes.

## Deploy on Reckitt's Azure workspace (after the SOW)

Use `deploy/reckitt-azure/app.yaml`. It sets ADLS storage, the SQL warehouse, `allow_pipeline_run: false` and an LLM serving endpoint.

1. Fill in the placeholders.
2. Add the secrets `adls-key`, `warehouse-token` and `llm-key` as app resources.
3. Grant the app read access on the landing zone, write access on its ADLS prefix, `SELECT` on bronze, and create/modify on the silver schema.

The mapping JSON is written as a Delta table to ADLS (`mapping_store/`) with delta-rs.

## Not in POC scope

- **Running Reckitt's pipeline from the app.** It is run by hand; the app only checks the result.
- **Scheduling.**
- **Value standards beyond units, currencies and flags.** Jonathan's second harmonisation type is still to be confirmed.

## Open items

- Pranav's walkthrough of how the pipeline runs the SQL and how harmonisation works today.
- The real catalog, schema and table names; ADLS and SQL warehouse access.
- The approved LLM endpoint, the real MDM extracts, and the full target schema.
- Veritas exports for the 5 POC retailers.
- Who the end user is (Reckitt or the retailers).
