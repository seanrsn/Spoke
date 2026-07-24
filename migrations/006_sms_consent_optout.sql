-- ============================================================================
-- Migration 006: SMS consent + opt-out tracking on customers
-- ============================================================================
--
-- WHAT THIS DOES
--   - customers.sms_consent      1/0/NULL — whether the customer agreed to
--                                 receive texts when they were created at the
--                                 counter (NULL for pre-existing customers,
--                                 who have implied consent from doing business).
--   - customers.sms_consent_at   when that consent was recorded.
--   - customers.sms_opted_out    1 after the customer texts STOP (or similar)
--                                 to the shop's number; 0 again on START.
--                                 Backend-Form refuses to queue invoice texts
--                                 to opted-out customers (TCPA compliance).
--
-- APPLY ORDER
--   Run BEFORE deploying the Lambdas that reference these columns.
-- ============================================================================

ALTER TABLE `customers`
  ADD COLUMN `sms_consent` TINYINT(1) NULL,
  ADD COLUMN `sms_consent_at` DATETIME NULL,
  ADD COLUMN `sms_opted_out` TINYINT(1) NOT NULL DEFAULT 0;
