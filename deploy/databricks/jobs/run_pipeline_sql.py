# Databricks notebook source
# MAGIC %md
# MAGIC # reckitt-pipeline-sim: run the generated mapping SQL
# MAGIC Stand-in for Reckitt's pipeline in the Nablon simulation. The onboarding app starts this job
# MAGIC after Gate A with two job parameters:
# MAGIC * `sql_path`: the run script on the volume (CREATE TABLE IF NOT EXISTS + each mapping's DELETE/INSERT)
# MAGIC * `session_id`: the onboarding session, for the log
# MAGIC
# MAGIC Each mapping's SQL deletes its own submission first, so a rerun is safe.

# COMMAND ----------

import json
import re

dbutils.widgets.text("sql_path", "")
dbutils.widgets.text("session_id", "")
sql_path = dbutils.widgets.get("sql_path").strip()
session_id = dbutils.widgets.get("session_id").strip()
if not sql_path.startswith("/Volumes/"):
    raise ValueError(f"sql_path must be a /Volumes/... path, got {sql_path!r}")

with open(sql_path, encoding="utf-8") as fh:
    script = fh.read()

body = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
statements = [s.strip().rstrip(";").strip() for s in re.split(r";\s*\n", body) if s.strip().strip(";")]
print(f"session {session_id}: {len(statements)} statements from {sql_path}")

# COMMAND ----------

loaded = {}
for i, stmt in enumerate(statements, 1):
    first = stmt.split("\n", 1)[0][:160]
    print(f"[{i}/{len(statements)}] {first}")
    df = spark.sql(stmt)
    m = re.match(r"INSERT\s+INTO\s+(\S+)", stmt, re.I)
    if m:
        rows = df.collect()
        n = rows[0]["num_inserted_rows"] if rows and "num_inserted_rows" in rows[0].asDict() else None
        loaded[m.group(1)] = n
        print(f"    inserted rows: {n}")

dbutils.notebook.exit(json.dumps({"session_id": session_id, "statements": len(statements), "inserted": loaded}))
