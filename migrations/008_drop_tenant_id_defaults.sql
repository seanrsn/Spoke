-- Migration 008: drop the DEFAULT '1' bridge from tenant_id columns
-- (2026-09-07) — multi-tenancy step 10 cleanup / pre-pilot hardening.
--
-- WHY
--   Migration 002 added tenant_id with DEFAULT '1' so pre-multi-tenant code
--   that inserted rows without a tenant kept working (silently filed under
--   Brooklyn Bikery). Every INSERT in all four Lambdas, provision_tenant.py
--   and the staging test suite now passes tenant_id explicitly. Keeping the
--   default means any future query that forgets it would quietly drop another
--   shop's data into tenant 1 — exactly the leak the isolation gate exists to
--   prevent. With no default, such a query fails loudly (NOT NULL violation).
--
-- SAFETY
--   Metadata-only change (no rewrite, no type change, FKs untouched). Safe to
--   run before OR after deploying the matching Lambda code — the code never
--   relied on the default. Rollback: re-add the default per table:
--     ALTER TABLE <t> ALTER COLUMN tenant_id SET DEFAULT '1';
--
-- RUN ORDER
--   staging first (python migrations/run_migration.py against
--   bikeshop-credentials-staging), then prod after the gate is green.

ALTER TABLE `customers`          ALTER COLUMN `tenant_id` DROP DEFAULT;
ALTER TABLE `orders`             ALTER COLUMN `tenant_id` DROP DEFAULT;
ALTER TABLE `messages`           ALTER COLUMN `tenant_id` DROP DEFAULT;
ALTER TABLE `push_subscriptions` ALTER COLUMN `tenant_id` DROP DEFAULT;
