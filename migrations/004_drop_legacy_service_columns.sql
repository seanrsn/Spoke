-- ============================================================================
-- Migration 004: Drop legacy service columns from `orders`
-- ============================================================================
--
-- WHAT THIS DOES
--   Drops the 40 boolean service columns, the 2 integer spoke-count columns,
--   and the custom_service trio (custom_service / custom_description /
--   custom_service_price) from the `orders` table. All this data now lives in
--   `order_services` (line items) joined to `service_catalog`.
--
-- WHY THIS IS SAFE NOW
--   - Step 4 (Backend-Form.py dual-write): every new order has been writing
--     to `order_services` since 2026-05-25.
--   - Step 3 + 3c (backfill + gap-fill): every historical order has
--     `order_services` rows summing to `orders.price`.
--   - Step 5 (Admin-Dashboard.py): the dashboard reads service data from
--     `order_services`, not these columns.
--   - Step 6a (Backend-Form.py): stopped writing these columns. Any new order
--     would have them at the DB defaults (0 / NULL) anyway.
--
-- IMPACT ON LIVE FLOW
--   - Backend-Form.py: no impact. order_updates dict no longer references
--     these columns.
--   - Admin-Dashboard.py search endpoint: no impact. SELECT doesn't reference
--     these columns.
--   - Admin-Dashboard.py /get-db-tables endpoint: returns `SELECT * FROM
--     orders` — the response payload shrinks by ~46 columns but doesn't crash.
--     If a debug page references those columns on the frontend, it'll render
--     them as undefined (graceful in JS).
--
-- SAFETY NET
--   RDS snapshot `pre-multitenant-step6-2026-05-25`. Restore creates a new
--   RDS instance (snapshot restore is non-destructive); to revert, update the
--   `host` field in the `bikeshop-credentials` Secrets Manager secret to point
--   at the restored instance and the Lambdas will pick it up on next cold
--   start.
--
-- THIS IS THE FIRST IRREVERSIBLE MIGRATION
--   Previous migrations were additive (could be rolled back with simple
--   DROP TABLE / DROP COLUMN). This one removes data. The order_services
--   table holds equivalent information, but the exact prior shape (boolean
--   per service) is only recoverable via snapshot restore.
-- ============================================================================

USE `bikeshop`;

-- ----------------------------------------------------------------------------
-- 1) Drop the 40 boolean service columns
-- ----------------------------------------------------------------------------

ALTER TABLE `orders`
  DROP COLUMN `front_flat`,
  DROP COLUMN `rear_flat`,
  DROP COLUMN `front_flat_ebike`,
  DROP COLUMN `rear_flat_ebike`,
  DROP COLUMN `front_brake_adj`,
  DROP COLUMN `rear_brake_adj`,
  DROP COLUMN `front_brake_adj_ebike`,
  DROP COLUMN `rear_brake_adj_ebike`,
  DROP COLUMN `front_replace_vbrake_pads`,
  DROP COLUMN `rear_replace_vbrake_pads`,
  DROP COLUMN `front_new_vbrake_pads`,
  DROP COLUMN `rear_new_vbrake_pads`,
  DROP COLUMN `front_replace_disc_pads`,
  DROP COLUMN `rear_replace_disc_pads`,
  DROP COLUMN `front_new_disc_pads`,
  DROP COLUMN `rear_new_disc_pads`,
  DROP COLUMN `front_hydraulic_brake_bleed`,
  DROP COLUMN `rear_hydraulic_brake_bleed`,
  DROP COLUMN `tune_up`,
  DROP COLUMN `front_derailleur_adj`,
  DROP COLUMN `rear_derailleur_adj`,
  DROP COLUMN `replace_cassette`,
  DROP COLUMN `new_bb`,
  DROP COLUMN `replace_chain`,
  DROP COLUMN `replace_crank_bb`,
  DROP COLUMN `replace_front_brake_line`,
  DROP COLUMN `replace_rear_brake_line`,
  DROP COLUMN `replace_front_gear_line`,
  DROP COLUMN `replace_rear_gear_line`,
  DROP COLUMN `front_wheel_true`,
  DROP COLUMN `rear_wheel_true`,
  DROP COLUMN `front_repack_wheel`,
  DROP COLUMN `rear_repack_wheel`,
  DROP COLUMN `replace_front_rotor`,
  DROP COLUMN `replace_rear_rotor`,
  DROP COLUMN `repack_headset`,
  DROP COLUMN `repack_headset_ebike`,
  DROP COLUMN `ebike_diagnostic`,
  DROP COLUMN `bike_assembly`,
  DROP COLUMN `ebike_assembly`;

-- ----------------------------------------------------------------------------
-- 2) Drop the spoke-count columns
-- ----------------------------------------------------------------------------

ALTER TABLE `orders`
  DROP COLUMN `front_fix_spoke`,
  DROP COLUMN `rear_fix_spoke`;

-- ----------------------------------------------------------------------------
-- 3) Drop the custom_service trio
-- ----------------------------------------------------------------------------

ALTER TABLE `orders`
  DROP COLUMN `custom_service`,
  DROP COLUMN `custom_description`,
  DROP COLUMN `custom_service_price`;

-- ----------------------------------------------------------------------------
-- 4) Verify
-- ----------------------------------------------------------------------------
-- After this migration, the `orders` table should have ONLY:
--   id, tenant_id, customer_id, date_of_service, bike_description,
--   customer_notes, backend_notes, price, final_price
--
-- 9 columns total, down from 54. All service data lives in `order_services`.
--
-- Sanity check:
--   SELECT COLUMN_NAME FROM information_schema.COLUMNS
--   WHERE TABLE_SCHEMA='bikeshop' AND TABLE_NAME='orders'
--   ORDER BY ORDINAL_POSITION;
-- ============================================================================
