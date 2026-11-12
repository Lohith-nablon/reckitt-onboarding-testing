# Databricks simulation: setup (Nablon workspace, catalog `nablon_poc`)

The app runs as a Databricks App under its own service principal (SP). It needs no token or config
file: Unity Catalog, the SQL warehouse and the Jobs API all use the SP's OAuth identity.

## What runs where

| Piece | Where | Name |
| --- | --- | --- |
| Landing zone (ADLS stand-in) | UC volume | `/Volumes/nablon_poc/onboarding/files/retailer-onboarding-poc/landing/<market>/<retailer>/` |
| File pickup | Job with file arrival trigger | `onboarding-intake` → `nablon_poc.onboarding.onboarding_landing_events` |
| RB codes (dummy) | UC tables | `nablon_poc.rb_master.rb_product_master`, `rb_customer_master` |
| Mapping JSON (Delta) | UC table | `nablon_poc.bronze.onboarding_mapping_store` |
| Pipeline (Reckitt stand-in) | Job started by the app after Gate A | `reckitt-pipeline-sim` → `nablon_poc.bronze.*` |
| Harmonised output | UC tables | `nablon_poc.silver.*_harmonised`, `retailer_code_crosswalk` |
| Audit log | UC table | `nablon_poc.onboarding.onboarding_audit` |

## One-time setup

1. **Pull** the Git folder so it has this commit.
2. **SQL editor:** run the top of `setup.sql` (catalog, schemas `onboarding`, `bronze`, `silver`, `rb_master`, volume).
3. **Create the pipeline job** `reckitt-pipeline-sim`:
   * Jobs & Pipelines → Create job → task type Notebook → path `deploy/databricks/jobs/run_pipeline_sql` in the Git folder,
     compute Serverless → Job parameters `sql_path` and `session_id` (empty defaults).
   * Or with the CLI: edit the notebook path in `jobs/pipeline_job.json`, then `databricks jobs create --json @deploy/databricks/jobs/pipeline_job.json`.
4. **Create the intake job** `onboarding-intake` the same way with `jobs/intake_job.json`
   (notebook `record_landing_files`, trigger = File arrival on `/Volumes/nablon_poc/onboarding/files/retailer-onboarding-poc/landing/`).
5. **App resources** (app → Edit → Resources):
   * SQL warehouse, key `sql-warehouse`, permission Can use (already there).
   * Job `reckitt-pipeline-sim`, key `pipeline-job`, permission **Can manage run**.
6. **Grants:** run the `GRANT` block in `setup.sql` with the app's SP application ID.
7. **Deploy** the app. `app.yaml` sets `POC_MDM__SOURCE=uc` and `POC_PIPELINE__MODE=job`.

## Demo run

1. Overview → **Load demo data** (files into the landing zone, RB codes into `rb_master`).
2. Overview → **Check Unity Catalog**: identity = the app SP, RB tables readable with 30 / 33 rows.
3. Optional: upload a file into `landing/UK/TESCO/` on the volume; within about a minute the intake job records it.
4. **New files waiting** → Start onboarding on `tesco_toolkit_daily_2026-10-11.csv` → AI Discovery and mapping run.
5. Mapping → check readiness (green / amber / red) → **Approve and publish** (Gate A).
6. Pipeline screen: the job run goes Queued → Running → Succeeded; Phase 2 starts by itself and stops at Gate B.
7. Harmonisation → Gate B → Validate → Sign off.
8. Start the same file again: known layout, Gates A and B pass on their own, straight to Validate.
9. **Metrics & KPIs**: auto-ready %, Veritas match, manual inputs, monthly roll-up, accuracy over time.

## Switches (app.yaml)

| Variable | Values |
| --- | --- |
| `POC_PIPELINE__MODE` | `job` (app starts the job) · `watch_table` (Reckitt starts it; app watches bronze) · `manual` (button) |
| `POC_PIPELINE__AUTO_PHASE2` | `true` / `false` |
| `POC_MDM__SOURCE` | `uc` (rb_master tables) · `files` (CSV in the volume) |
| `POC_OPS__AUDIT` | `auto` (on with the Databricks warehouse) · `on` · `off` |
| `POC_METRICS__GREEN` / `POC_METRICS__AMBER` | RAG thresholds, default 0.90 / 0.70 |

## If something fails

| Symptom | Cause |
| --- | --- |
| Deploy fails on `pipeline-job` | Job resource not added to the app (step 5) |
| Pipeline run "could not start … PERMISSION_DENIED" | SP lacks Can manage run on the job |
| Job fails on `read_files` / table not found | Job owner lacks access to the volume or `nablon_poc.bronze` |
| Check Unity Catalog: RB tables "error" | Missing `USE SCHEMA` / `SELECT` on `nablon_poc.rb_master` |
| New files never show "recorded by intake job" | Intake job trigger paused, or the path does not end in `/landing/` |
