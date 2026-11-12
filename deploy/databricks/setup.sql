-- One-time setup for the simulation on Nablon's Databricks workspace.
-- Run in the SQL editor on the same SQL warehouse the app will use.
-- Uses catalog nablon_poc; if you use another catalog, replace nablon_poc here and in app.yaml.

CREATE CATALOG IF NOT EXISTS nablon_poc;        -- skip if you are not allowed to create catalogs; use an existing one
CREATE SCHEMA  IF NOT EXISTS nablon_poc.onboarding;   -- holds the volume (stand-in for ADLS)
CREATE SCHEMA  IF NOT EXISTS nablon_poc.bronze;       -- the pipeline loads here (stand-in for Reckitt bronze)
CREATE SCHEMA  IF NOT EXISTS nablon_poc.silver;       -- harmonised output (stand-in for Reckitt silver)
CREATE VOLUME  IF NOT EXISTS nablon_poc.onboarding.files;
CREATE SCHEMA  IF NOT EXISTS nablon_poc.rb_master;    -- dummy RB codes (stand-in for Reckitt's RB master tables)

-- After creating the app: grant its service principal access.
-- Find the principal on the app's Authorization tab (an application ID like 1a2b3c4d-...).
-- Replace <app-sp> below, keep the backticks.
GRANT USE CATALOG ON CATALOG nablon_poc TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA nablon_poc.bronze TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA nablon_poc.silver TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA nablon_poc.onboarding TO `<app-sp>`;   -- audit + landing events
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA nablon_poc.rb_master TO `<app-sp>`;    -- MODIFY only so "Load demo data" can (re)load the dummy RB codes
GRANT READ VOLUME, WRITE VOLUME ON VOLUME nablon_poc.onboarding.files TO `<app-sp>`;

-- Optional: load the dummy RB codes yourself instead of with the app's "Load demo data" button.
-- Run "Load demo data" once first so the CSVs are on the volume (mdm/ folder), then:
-- CREATE OR REPLACE TABLE nablon_poc.rb_master.rb_product_master AS
--   SELECT * FROM read_files('/Volumes/nablon_poc/onboarding/files/retailer-onboarding-poc/mdm/erp_product_master.csv',
--                            format => 'csv', header => true, inferSchema => false);
-- CREATE OR REPLACE TABLE nablon_poc.rb_master.rb_customer_master AS
--   SELECT * FROM read_files('/Volumes/nablon_poc/onboarding/files/retailer-onboarding-poc/mdm/rb_customer_master.csv',
--                            format => 'csv', header => true, inferSchema => false);

-- After a full run in the app, check the results:
-- SELECT * FROM nablon_poc.bronze.onboarding_mapping_store;            -- mapping JSON in Delta format
-- SELECT count(*) FROM nablon_poc.bronze.pos_sellout;                  -- loaded by the generated SQL
-- SELECT count(*) FROM nablon_poc.silver.pos_sellout_harmonised;       -- harmonised output
-- SELECT entity, decision, count(*) FROM nablon_poc.silver.retailer_code_crosswalk GROUP BY ALL;
-- SELECT * FROM nablon_poc.rb_master.rb_product_master;                -- dummy RB product codes
-- SELECT * FROM nablon_poc.rb_master.rb_customer_master;               -- dummy RB outlet codes
-- SELECT * FROM nablon_poc.onboarding.onboarding_landing_events ORDER BY arrived_at DESC;   -- files picked up
-- SELECT session_id, module, step, decided_by, at FROM nablon_poc.onboarding.onboarding_audit ORDER BY at DESC;  -- audit log
-- SELECT decided_by, count(*) FROM nablon_poc.onboarding.onboarding_audit GROUP BY ALL;     -- AI vs people
