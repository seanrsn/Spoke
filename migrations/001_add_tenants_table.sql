-- ============================================================================
-- Migration 001: Add tenants table + insert Brooklyn Bikery as tenant 1
-- ============================================================================
--
-- WHAT THIS DOES
--   Creates the `tenants` table that holds per-shop config (branding, Twilio
--   credentials, tax rate, CORS origin, admin auth). Inserts one row for
--   Brooklyn Bikery so the existing shop becomes tenant_id = 1.
--
-- IMPACT ON EXISTING WORKFLOW
--   NONE. This is pure additive — no existing tables are modified, no Lambda
--   code reads from `tenants` yet. Brooklyn Bikery continues to use the
--   hardcoded secret IDs in Lambda code until step 7 of the migration plan.
--
-- SAFETY NET
--   RDS snapshot `pre-multitenant-step1-2026-05-17` taken before running this.
--   1-click restore is available via AWS console → RDS → Snapshots if anything
--   goes wrong.
--
-- ROLLBACK
--   DROP TABLE tenants;
--   (Safe — no FK references to this table exist yet.)
-- ============================================================================

USE `bikeshop`;

-- ----------------------------------------------------------------------------
-- 1) Create the tenants table
--
-- Notes on schema choices:
--   - No `admin_username` column. The existing admin login flow checks only a
--     password (no username), and in the path-based-routing model each tenant
--     is identified by the URL path. Adding a username field would be dead
--     weight until/unless multi-user-per-tenant becomes a thing.
--   - No `jwt_signing_secret_arn` column. Per Decision 3, a single master
--     JWT secret is used across all tenants with `tenant_id` baked into the
--     claim. The existing `bikery-jwt-secret` becomes that master secret.
--   - `allowed_origin` is per-tenant for forward-compatibility with subdomain
--     routing, even though path-based routing on a single domain makes the
--     value identical across tenants in v1.
-- ----------------------------------------------------------------------------

CREATE TABLE `tenants` (
  `id` int NOT NULL AUTO_INCREMENT,
  `slug` varchar(50) NOT NULL,                       -- URL-safe identifier, e.g. 'brooklyn-bikery'
  `display_name` varchar(100) NOT NULL,              -- e.g. 'Brooklyn Bikery'
  `phone` varchar(20) DEFAULT NULL,                  -- shop contact phone (for invoice footer)
  `address` text,                                    -- shop address (for invoice footer)
  `tax_rate` decimal(5,4) NOT NULL DEFAULT '0.0875', -- 8.75% NYC default; override per tenant
  `allowed_origin` varchar(255) NOT NULL,            -- CORS origin (e.g. 'https://brooklynbikery.com')

  -- Twilio config (per-tenant: each shop has its own Twilio account)
  `twilio_account_sid` varchar(100) NOT NULL,
  `twilio_auth_token_secret_arn` varchar(255) NOT NULL,
  `twilio_from_number` varchar(20) NOT NULL,
  `sms_sender_name` varchar(50) NOT NULL,            -- branding for SMS messages

  -- Admin auth (per-tenant: each shop has its own admin password secret)
  `admin_password_secret_arn` varchar(255) NOT NULL,

  `status` enum('active','suspended') NOT NULL DEFAULT 'active',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  UNIQUE KEY `slug` (`slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- 2) Insert Brooklyn Bikery as tenant 1
--
-- phone + address are NULL for now (Sean can UPDATE them later — they're only
-- used for invoice footer aesthetics and no Lambda logic depends on them).
-- ----------------------------------------------------------------------------

INSERT INTO `tenants` (
  `slug`,
  `display_name`,
  `phone`,
  `address`,
  `tax_rate`,
  `allowed_origin`,
  `twilio_account_sid`,
  `twilio_auth_token_secret_arn`,
  `twilio_from_number`,
  `sms_sender_name`,
  `admin_password_secret_arn`,
  `status`
) VALUES (
  'brooklyn-bikery',
  'Brooklyn Bikery',
  NULL,
  NULL,
  0.0875,
  'https://brooklynbikery.com',
  '<REDACTED_TWILIO_SID>',   -- Real value lives in prod DB; pulled from twilio-credentials secret at provisioning time.
  'arn:aws:secretsmanager:us-east-1:807373873973:secret:twilio-credentials-1rOdau',
  '<REDACTED_TWILIO_FROM>',  -- Real value lives in prod DB; pulled from twilio-credentials secret.
  'Brooklyn Bikery',
  'arn:aws:secretsmanager:us-east-1:807373873973:secret:bikery-admin-password-pfYdDg',
  'active'
);

-- NOTE: The literal Twilio SID and from-number values that ran in prod have been
-- redacted from this file. GitHub push protection treats Twilio SIDs as
-- secret-shaped and blocks them. The actual values were pulled from the
-- twilio-credentials secret at migration time and live in the prod DB now.
-- Future tenants should be provisioned via `provision_tenant.py` (step 9),
-- which reads per-tenant Twilio config from Secrets Manager — never hardcoded
-- in committed files.

-- ----------------------------------------------------------------------------
-- 3) Verify
-- ----------------------------------------------------------------------------
-- After the INSERT, this query should return exactly one row:
--
--   SELECT id, slug, display_name, tax_rate, allowed_origin, status
--   FROM tenants;
--
-- Expected: id=1, slug='brooklyn-bikery', tax_rate=0.0875,
--           allowed_origin='https://brooklynbikery.com', status='active'.
-- ============================================================================
