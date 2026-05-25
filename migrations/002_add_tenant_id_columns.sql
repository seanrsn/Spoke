-- ============================================================================
-- Migration 002: Add tenant_id columns to customers / orders / messages /
--                push_subscriptions
-- ============================================================================
--
-- WHAT THIS DOES
--   Adds a `tenant_id INT NOT NULL DEFAULT 1` column to every domain table.
--   Adds a foreign key from each new column to `tenants(id)`.
--   Swaps `customers.UNIQUE phone` for a composite `UNIQUE (tenant_id, phone)`
--   so future tenants can have customers with the same phone numbers without
--   collision.
--
-- WHY DEFAULT 1
--   Existing Lambda code (Customer-Form.py, Backend-Form.py, Admin-Dashboard.py,
--   Send-SMS.py) inserts rows without a `tenant_id` value. With DEFAULT 1, those
--   INSERTs continue to work — every row silently gets attributed to
--   tenant 1 (Brooklyn Bikery). Without the default, every INSERT would fail
--   the moment NOT NULL is enforced. The default is a temporary bridge.
--
--   It MUST be removed in step 10 (right before provisioning a second real
--   tenant) so that any Lambda not yet updated would fail loudly instead of
--   silently writing to tenant 1. See `migrations/010_remove_tenant_id_defaults.sql`
--   (not yet written).
--
-- IMPACT ON EXISTING WORKFLOW
--   None observable. New rows continue to insert with tenant_id=1 via the
--   default. Existing rows get backfilled to tenant_id=1 by the same default.
--   The customers.phone UNIQUE swap is functionally equivalent in single-tenant
--   land (still: one row per (1, phone)).
--
-- SAFETY NET
--   RDS snapshot `pre-multitenant-step2-2026-05-18`. 1-click restore available.
--
-- MYSQL DDL CAVEAT
--   ALTER TABLE / ADD CONSTRAINT statements are implicitly committed in MySQL.
--   The Python runner's transaction wrapping won't roll back a partially-
--   applied migration. If statement N fails, statements 1..N-1 are already
--   committed. Recovery path: restore from the pre-step2 snapshot.
--
-- ROLLBACK (if needed before step 3 runs)
--   ALTER TABLE customers DROP FOREIGN KEY fk_customers_tenant;
--   ALTER TABLE customers DROP INDEX tenant_phone;
--   ALTER TABLE customers ADD UNIQUE KEY phone (phone);
--   ALTER TABLE customers DROP COLUMN tenant_id;
--   (Repeat for orders, messages, push_subscriptions, without the phone-index
--   swap.)
-- ============================================================================

USE `bikeshop`;

-- ----------------------------------------------------------------------------
-- 1) customers: add tenant_id, swap phone UNIQUE, add FK
-- ----------------------------------------------------------------------------

ALTER TABLE `customers`
  ADD COLUMN `tenant_id` int NOT NULL DEFAULT 1 AFTER `id`;

ALTER TABLE `customers`
  DROP INDEX `phone`;

ALTER TABLE `customers`
  ADD UNIQUE KEY `tenant_phone` (`tenant_id`, `phone`);

ALTER TABLE `customers`
  ADD CONSTRAINT `fk_customers_tenant`
  FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`);

-- ----------------------------------------------------------------------------
-- 2) orders: add tenant_id, add FK
-- ----------------------------------------------------------------------------

ALTER TABLE `orders`
  ADD COLUMN `tenant_id` int NOT NULL DEFAULT 1 AFTER `id`;

ALTER TABLE `orders`
  ADD CONSTRAINT `fk_orders_tenant`
  FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`);

-- ----------------------------------------------------------------------------
-- 3) messages: add tenant_id, add FK
-- ----------------------------------------------------------------------------

ALTER TABLE `messages`
  ADD COLUMN `tenant_id` int NOT NULL DEFAULT 1 AFTER `id`;

ALTER TABLE `messages`
  ADD CONSTRAINT `fk_messages_tenant`
  FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`);

-- ----------------------------------------------------------------------------
-- 4) push_subscriptions: add tenant_id, add FK
-- ----------------------------------------------------------------------------

ALTER TABLE `push_subscriptions`
  ADD COLUMN `tenant_id` int NOT NULL DEFAULT 1 AFTER `id`;

ALTER TABLE `push_subscriptions`
  ADD CONSTRAINT `fk_push_subscriptions_tenant`
  FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`);

-- ----------------------------------------------------------------------------
-- 5) Verify
-- ----------------------------------------------------------------------------
-- All four tables should now have tenant_id NOT NULL with FK to tenants(id),
-- all existing rows populated with tenant_id=1, customers.phone UNIQUE swapped
-- for composite (tenant_id, phone).
--
-- Sanity checks:
--   SELECT COUNT(*), COUNT(tenant_id), MIN(tenant_id), MAX(tenant_id) FROM customers;
--   SELECT COUNT(*), COUNT(tenant_id), MIN(tenant_id), MAX(tenant_id) FROM orders;
--   SELECT COUNT(*), COUNT(tenant_id), MIN(tenant_id), MAX(tenant_id) FROM messages;
--   SELECT COUNT(*), COUNT(tenant_id), MIN(tenant_id), MAX(tenant_id) FROM push_subscriptions;
-- All should show MIN=1, MAX=1, COUNT(tenant_id) = COUNT(*).
-- ============================================================================
