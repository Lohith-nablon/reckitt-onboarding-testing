# Databricks notebook source
# MAGIC %md
# MAGIC # onboarding-intake: record new files in the landing zone
# MAGIC Runs on a **file arrival trigger** on the landing folder of the volume (stand-in for ADLS).
# MAGIC The trigger does not say which file arrived, so this lists the folder and records every file
# MAGIC not seen before in `onboarding_landing_events`. The app shows them under **New files waiting**.
# MAGIC Folder layout: `landing/<market>/<retailer>/<file>`.

# COMMAND ----------

dbutils.widgets.text("landing_path", "/Volumes/nablon_poc/onboarding/files/retailer-onboarding-poc/landing")
dbutils.widgets.text("events_table", "nablon_poc.onboarding.onboarding_landing_events")
landing = dbutils.widgets.get("landing_path").rstrip("/")
table = dbutils.widgets.get("events_table")

ALLOWED = (".csv", ".txt", ".tsv", ".xlsx", ".xls", ".xlsm", ".json", ".parquet")


# The folder exists only once a file has landed (or "Load demo data" ran). Create it so the
# file arrival trigger has something to watch, and record nothing this run.
try:
    dbutils.fs.ls(landing)
except Exception as exc:
    if "FileNotFound" not in str(exc) and "No such file" not in str(exc):
        raise
    dbutils.fs.mkdirs(landing)
    print(f"created empty landing folder {landing}")


def walk(path):
    for f in dbutils.fs.ls(path):
        if f.isDir():
            yield from walk(f.path)
        elif f.name.lower().endswith(ALLOWED):
            yield f

# COMMAND ----------

from datetime import datetime, timezone

rows = []
for f in walk(landing):
    path = f.path.replace("dbfs:", "")
    rel = path[len(landing) + 1:].split("/")
    rows.append({
        "file_path": path, "file_name": f.name,
        "market": rel[0].upper() if len(rel) > 2 else None,
        "retailer": rel[1].upper() if len(rel) > 2 else None,
        "size_bytes": str(f.size),
        "modification_time": datetime.fromtimestamp(f.modificationTime / 1000, timezone.utc).isoformat(timespec="seconds"),
        "arrived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
print(f"{len(rows)} files under {landing}")

spark.sql(f"""CREATE TABLE IF NOT EXISTS {table} (
  file_path STRING, file_name STRING, market STRING, retailer STRING, size_bytes STRING,
  modification_time STRING, arrived_at STRING)""")
if rows:
    spark.createDataFrame(rows).createOrReplaceTempView("new_files")
    spark.sql(f"""MERGE INTO {table} t USING new_files n ON t.file_path = n.file_path
      WHEN NOT MATCHED THEN INSERT (file_path, file_name, market, retailer, size_bytes, modification_time, arrived_at)
      VALUES (n.file_path, n.file_name, n.market, n.retailer, n.size_bytes, n.modification_time, n.arrived_at)""")
display(spark.sql(f"SELECT * FROM {table} ORDER BY arrived_at DESC"))
