-- One-time setup for catalog `nablon-testing`, schema reckitt_onboarding (matches app.yaml).
-- The hyphen means the catalog name must be in backticks in every SQL statement.
-- Bronze tables (pos_sellout, ...) and silver tables (*_harmonised) share the one schema.

CREATE SCHEMA IF NOT EXISTS `nablon-testing`.reckitt_onboarding;
CREATE VOLUME IF NOT EXISTS `nablon-testing`.reckitt_onboarding.files;   -- stand-in for ADLS

-- After creating the app: replace <app-sp> with the app's service principal application ID (keep the backticks).
GRANT USE CATALOG ON CATALOG `nablon-testing` TO `<app-sp>`;
GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA `nablon-testing`.reckitt_onboarding TO `<app-sp>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME `nablon-testing`.reckitt_onboarding.files TO `<app-sp>`;

-- Checks after a full run:
-- SELECT * FROM `nablon-testing`.reckitt_onboarding.onboarding_mapping_store;      -- mapping JSON in Delta
-- SELECT count(*) FROM `nablon-testing`.reckitt_onboarding.pos_sellout;            -- bronze, loaded by the generated SQL
-- SELECT count(*) FROM `nablon-testing`.reckitt_onboarding.pos_sellout_harmonised; -- silver
-- SELECT entity, decision, count(*) FROM `nablon-testing`.reckitt_onboarding.retailer_code_crosswalk GROUP BY ALL;
