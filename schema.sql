-- Brooklyn Bikery Database Schema
-- MySQL database structure for service tracking, multi-tenant capable.
--
-- This file represents the current desired state of the schema. Apply
-- migrations under `migrations/` in numbered order to reach this state from
-- a fresh DB.
--
-- Migration history:
--   001_add_tenants_table.sql                 (2026-05-17) — added tenants table
--   002_add_tenant_id_columns.sql             (2026-05-18) — added tenant_id to
--     customers, orders, messages, push_subscriptions (NOT NULL DEFAULT 1, FK
--     to tenants). Swapped customers.UNIQUE phone for composite UNIQUE
--     (tenant_id, phone).
--   003_add_service_catalog.sql               (2026-05-18) — created
--     service_catalog (43 rows seeded for Brooklyn Bikery, tenant 1) and
--     order_services. Backfilled 266 order_services rows from the 60 existing
--     orders' legacy boolean columns (via 003b_backfill_order_services.py),
--     including reconciliation entries that captured historical price-vs-
--     boolean discrepancies.
--   003c_gap_fill_order_services.py           (2026-05-25) — gap-filled
--     order_services for 3 orders placed between 003b and step 4's deploy.
--   004_drop_legacy_service_columns.sql       (2026-05-25) — DROP'd 45 legacy
--     columns from `orders` (40 boolean service columns, 2 spoke counts,
--     custom_service/custom_description/custom_service_price). All service
--     data lives in `order_services` now. First IRREVERSIBLE migration —
--     restore from the `pre-multitenant-step6-2026-05-25` RDS snapshot if
--     rollback is required.

CREATE DATABASE IF NOT EXISTS `bikeshop`
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE `bikeshop`;

-- ----------------------------------------------------------------------------
-- tenants — per-shop config. Brooklyn Bikery is tenant_id = 1.
--
-- The DEFAULT '1' on tenant_id columns in other tables is a temporary bridge
-- for the multi-tenant migration (lets pre-multi-tenant Lambda code keep
-- inserting without specifying tenant_id). Removed in step 10, before any
-- second tenant is provisioned.
-- ----------------------------------------------------------------------------

CREATE TABLE `tenants` (
  `id` int NOT NULL AUTO_INCREMENT,
  `slug` varchar(50) NOT NULL,
  `display_name` varchar(100) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `address` text,
  `tax_rate` decimal(5,4) NOT NULL DEFAULT '0.0875',
  `allowed_origin` varchar(255) NOT NULL,
  `twilio_account_sid` varchar(100) NOT NULL,
  `twilio_auth_token_secret_arn` varchar(255) NOT NULL,
  `twilio_from_number` varchar(20) NOT NULL,
  `sms_sender_name` varchar(50) NOT NULL,
  `admin_password_secret_arn` varchar(255) NOT NULL,
  `status` enum('active','suspended') NOT NULL DEFAULT 'active',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `slug` (`slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- customers — one row per shop customer. Same phone number can exist at
-- different shops (composite UNIQUE (tenant_id, phone)).
-- ----------------------------------------------------------------------------

CREATE TABLE `customers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL DEFAULT '1',
  `name` varchar(100) DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `date_created` date DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tenant_phone` (`tenant_id`, `phone`),
  CONSTRAINT `fk_customers_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- orders — one row per service visit. Service line items live in
-- `order_services`; this table just carries metadata + denormalized totals.
-- ----------------------------------------------------------------------------

CREATE TABLE `orders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL DEFAULT '1',
  `customer_id` int DEFAULT NULL,
  `date_of_service` date DEFAULT NULL,
  `bike_description` varchar(255) DEFAULT NULL,
  `customer_notes` text,
  `backend_notes` text,
  `price` decimal(10,2) DEFAULT NULL,         -- subtotal (sum of order_services.price_charged)
  `final_price` decimal(10,2) DEFAULT NULL,   -- total with tenant tax_rate applied
  PRIMARY KEY (`id`),
  KEY `customer_id` (`customer_id`),
  KEY `fk_orders_tenant` (`tenant_id`),
  CONSTRAINT `fk_orders_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `orders_ibfk_1`
    FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- service_catalog — per-tenant list of offered services with default prices
-- and pricing formula. Codes are stable identifiers used by Lambda code to
-- recognize special services (spokes, custom).
-- ----------------------------------------------------------------------------

CREATE TABLE `service_catalog` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL,
  `code` varchar(50) NOT NULL,                       -- e.g. 'front_flat'
  `display_name` varchar(100) NOT NULL,              -- e.g. 'Front Flat Repair'
  `default_price` decimal(8,2) DEFAULT NULL,         -- NULL for 'custom' formula
  `pricing_formula` varchar(20) NOT NULL DEFAULT 'fixed',  -- 'fixed' | 'spoke' | 'custom'
  `category` varchar(50) DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `sort_order` int NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `tenant_code` (`tenant_id`, `code`),
  CONSTRAINT `fk_service_catalog_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- order_services — line items linking orders to services performed.
-- quantity > 1 for spoke services (price = 33 + 2*qty). price_charged is a
-- snapshot at order time, immune to future price changes in service_catalog.
-- ----------------------------------------------------------------------------

CREATE TABLE `order_services` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL,
  `order_id` int NOT NULL,
  `service_catalog_id` int NOT NULL,
  `quantity` int NOT NULL DEFAULT '1',
  `price_charged` decimal(8,2) NOT NULL,
  `notes` varchar(255) DEFAULT NULL,                 -- e.g. custom service description
  PRIMARY KEY (`id`),
  KEY `tenant_order` (`tenant_id`, `order_id`),
  KEY `service_catalog_id` (`service_catalog_id`),
  KEY `fk_order_services_order` (`order_id`),
  CONSTRAINT `fk_order_services_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `fk_order_services_order`
    FOREIGN KEY (`order_id`) REFERENCES `orders` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_order_services_catalog`
    FOREIGN KEY (`service_catalog_id`) REFERENCES `service_catalog` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- messages — inbound and outbound SMS history (Twilio).
-- ----------------------------------------------------------------------------

CREATE TABLE `messages` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL DEFAULT '1',
  `phone` varchar(20) NOT NULL,
  `direction` enum('inbound','outbound') NOT NULL,
  `body` text NOT NULL,
  `status` enum('received','queued','sent','delivered','failed') NOT NULL DEFAULT 'queued',
  `twilio_sid` varchar(64) DEFAULT NULL,
  `from_number` varchar(32) NOT NULL,
  `to_number` varchar(32) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_twilio_sid` (`twilio_sid`),
  KEY `idx_phone_created` (`phone`, `created_at`),
  KEY `fk_messages_tenant` (`tenant_id`),
  CONSTRAINT `fk_messages_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- push_subscriptions — web push notification endpoints per tenant admin.
-- ----------------------------------------------------------------------------

CREATE TABLE `push_subscriptions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL DEFAULT '1',
  `endpoint` varchar(500) NOT NULL,
  `p256dh` varchar(255) NOT NULL,
  `auth` varchar(255) NOT NULL,
  `user_agent` varchar(255) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `endpoint` (`endpoint`),
  KEY `idx_endpoint` (`endpoint`),
  KEY `fk_push_subscriptions_tenant` (`tenant_id`),
  CONSTRAINT `fk_push_subscriptions_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
