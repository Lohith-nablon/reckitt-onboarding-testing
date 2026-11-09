-- One-time setup for the simulation on Nablon's Databricks workspace.
-- Run in the SQL editor on the same SQL warehouse the app will use.
-- Uses catalog 'nablon-testing'; if you use another catalog, replace 'nablon-testing' here and in app.yaml.

CREATE CATALOG IF NOT EXISTS 'nablon-testing';        -- skip if you are not allowed to create catalogs; use an existing one
CREATE SCHEMA  IF NOT EXISTS 'nablon-testing'.reckitt_onboarding;   -- holds the volume (stand-in for ADLS)
CREATE SCHEMA  IF NOT EXISTS 'nablon-testing'.bronze;       -- the pipeline loads here (stand-in for Reckitt bronze)
CREATE SCHEMA  IF NOT EXISTS 'nablon-testing'.silver;       -- harmonised output (stand-in for Reckitt silver)
CREATE VOLUME  IF NOT EXISTS 'nablon-testing'.reckitt_onboarding.files;

-- After creating the app: grant its service principal access.
-- Find the principal on the app's Authorization tab (an application ID like 1a2b3c4d-...).
-- Replace <app-sp> below, keep the backticks.
GRANT USE CATALOG ON CATALOG 'nablon-testing' TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA 'nablon-testing'.bronze TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA 'nablon-testing'.silver TO `<app-sp>`;
GRANT USE SCHEMA ON SCHEMA 'nablon-testing'.reckitt_onboarding TO `<app-sp>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME 'nablon-testing'.reckitt_onboarding.files TO `<app-sp>`;

-- After a full run in the app, check the results:
-- SELECT * FROM 'nablon-testing'.bronze.onboarding_mapping_store;            -- mapping JSON in Delta format
-- SELECT count(*) FROM 'nablon-testing'.bronze.pos_sellout;                  -- loaded by the generated SQL
-- SELECT count(*) FROM 'nablon-testing'.silver.pos_sellout_harmonised;       -- harmonised output
-- SELECT entity, decision, count(*) FROM 'nablon-testing'.silver.retailer_code_crosswalk GROUP BY ALL;
