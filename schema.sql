-- Brooklyn Bikery Database Schema
-- MySQL database structure for customer management and service tracking
--
-- Migration history:
--   001_add_tenants_table.sql (2026-05-17) — added tenants table for multi-tenancy
--   002_add_tenant_id_columns.sql (2026-05-18) — added tenant_id to customers,
--     orders, messages, push_subscriptions (NOT NULL DEFAULT 1, FK to tenants).
--     Swapped customers.UNIQUE phone for composite UNIQUE (tenant_id, phone).
--   003_add_service_catalog.sql + 003b_backfill_order_services.py (2026-05-18) —
--     created service_catalog (43 rows seeded for Brooklyn Bikery, tenant 1) and
--     order_services (266 rows backfilled from existing 60 orders, including 21
--     reconciliation rows that capture historical price-vs-boolean discrepancies).
--     Lambdas still read/write the legacy boolean columns on `orders` — service
--     catalog reads/writes get wired up in step 4 (dual-write) and step 5
--     (read switch).
--
-- NOTE: this file is partially stale vs. prod. Prod has tables `messages` and
-- `push_subscriptions` that were never in this schema file; prod does NOT have
-- a `payments` table (pricing columns live on `orders`). A schema reconciliation
-- pass is on the todo list.

-- Create database
CREATE DATABASE IF NOT EXISTS `bikeshop`
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_0900_ai_ci;

USE `bikeshop`;

-- Tenants table
-- Per-shop config (branding, Twilio creds, tax rate, CORS, admin auth).
-- Brooklyn Bikery is tenant_id=1.
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

-- Customers table
-- Stores customer contact information with unique phone constraint
CREATE TABLE `customers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `date_created` date DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Orders table
-- Tracks service requests with front/rear component separation
-- Boolean fields (tinyint) indicate which services were performed
CREATE TABLE `orders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `customer_id` int DEFAULT NULL,
  `date_of_service` date DEFAULT NULL,
  `bike_description` varchar(255) DEFAULT NULL,
  
  -- Flat tire repairs
  `front_flat` tinyint(1) DEFAULT '0',
  `rear_flat` tinyint(1) DEFAULT '0',
  `front_flat_ebike` tinyint(1) DEFAULT '0',
  `rear_flat_ebike` tinyint(1) DEFAULT '0',
  
  -- Tune-up
  `tune_up` tinyint(1) DEFAULT '0',
  
  -- Brake adjustments
  `front_brake_adj` tinyint(1) DEFAULT '0',
  `rear_brake_adj` tinyint(1) DEFAULT '0',
  `front_brake_adj_ebike` tinyint(1) DEFAULT '0',
  `rear_brake_adj_ebike` tinyint(1) DEFAULT '0',
  
  -- Brake pad services
  `front_replace_vbrake_pads` tinyint(1) DEFAULT '0',
  `rear_replace_vbrake_pads` tinyint(1) DEFAULT '0',
  `front_new_vbrake_pads` tinyint(1) DEFAULT '0',
  `rear_new_vbrake_pads` tinyint(1) DEFAULT '0',
  `front_replace_disc_pads` tinyint(1) DEFAULT '0',
  `rear_replace_disc_pads` tinyint(1) DEFAULT '0',
  `front_new_disc_pads` tinyint(1) DEFAULT '0',
  `rear_new_disc_pads` tinyint(1) DEFAULT '0',
  `front_hydraulic_brake_bleed` tinyint(1) DEFAULT '0',
  `rear_hydraulic_brake_bleed` tinyint(1) DEFAULT '0',
  
  -- Derailleur and shifting
  `front_derailleur_adj` tinyint(1) DEFAULT '0',
  `rear_derailleur_adj` tinyint(1) DEFAULT '0',
  
  -- Wheel services
  `front_wheel_true` tinyint(1) DEFAULT '0',
  `rear_wheel_true` tinyint(1) DEFAULT '0',
  `front_repack_wheel` tinyint(1) DEFAULT '0',
  `rear_repack_wheel` tinyint(1) DEFAULT '0',
  
  -- Drivetrain
  `replace_crank_bb` tinyint(1) DEFAULT '0',
  `new_bb` tinyint(1) DEFAULT '0',
  `replace_chain` tinyint(1) DEFAULT '0',
  `replace_cassette` tinyint(1) DEFAULT '0',
  
  -- Cables and lines
  `replace_front_brake_line` tinyint(1) DEFAULT '0',
  `replace_rear_brake_line` tinyint(1) DEFAULT '0',
  `replace_front_gear_line` tinyint(1) DEFAULT '0',
  `replace_rear_gear_line` tinyint(1) DEFAULT '0',
  
  -- Headset
  `repack_headset` tinyint(1) DEFAULT '0',
  `repack_headset_ebike` tinyint(1) DEFAULT '0',
  
  -- Disc brake rotors
  `replace_front_rotor` tinyint(1) DEFAULT '0',
  `replace_rear_rotor` tinyint(1) DEFAULT '0',
  
  -- E-bike and assembly
  `ebike_diagnostic` tinyint(1) DEFAULT '0',
  `bike_assembly` tinyint(1) DEFAULT '0',
  `ebike_assembly` tinyint(1) DEFAULT '0',
  
  -- Spoke repairs (integer count, pricing: $35 base + $2 per spoke)
  `front_fix_spoke` int DEFAULT '0',
  `rear_fix_spoke` int DEFAULT '0',
  
  -- Custom services
  `custom_service` tinyint(1) DEFAULT '0',
  `custom_description` varchar(255) DEFAULT NULL,
  
  -- Notes
  `customer_notes` text,
  `backend_notes` text,
  
  PRIMARY KEY (`id`),
  KEY `customer_id` (`customer_id`),
  CONSTRAINT `orders_ibfk_1` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Payments table
-- Stores pricing for each service performed
-- ID matches order ID (1-to-1 relationship)
CREATE TABLE `payments` (
  `id` int NOT NULL AUTO_INCREMENT,
  
  -- Flat tire repair prices
  `front_flat_price` decimal(6,2) DEFAULT NULL,
  `rear_flat_price` decimal(6,2) DEFAULT NULL,
  `front_flat_ebike_price` decimal(6,2) DEFAULT NULL,
  `rear_flat_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Tune-up price
  `tune_up_price` decimal(6,2) DEFAULT NULL,
  
  -- Brake adjustment prices
  `front_brake_adj_price` decimal(6,2) DEFAULT NULL,
  `rear_brake_adj_price` decimal(6,2) DEFAULT NULL,
  `front_brake_adj_ebike_price` decimal(6,2) DEFAULT NULL,
  `rear_brake_adj_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Brake pad prices
  `front_replace_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_replace_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `front_new_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_new_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `front_replace_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_replace_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `front_new_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_new_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `front_hydraulic_brake_bleed_price` decimal(6,2) DEFAULT NULL,
  `rear_hydraulic_brake_bleed_price` decimal(6,2) DEFAULT NULL,
  
  -- Derailleur prices
  `front_derailleur_adj_price` decimal(6,2) DEFAULT NULL,
  `rear_derailleur_adj_price` decimal(6,2) DEFAULT NULL,
  
  -- Wheel service prices
  `front_wheel_true_price` decimal(6,2) DEFAULT NULL,
  `rear_wheel_true_price` decimal(6,2) DEFAULT NULL,
  `front_repack_wheel_price` decimal(10,2) DEFAULT NULL,
  `rear_repack_wheel_price` decimal(10,2) DEFAULT NULL,
  
  -- Drivetrain prices
  `replace_crank_bb_price` decimal(6,2) DEFAULT NULL,
  `new_bb_price` decimal(6,2) DEFAULT NULL,
  `replace_chain_price` decimal(6,2) DEFAULT NULL,
  `replace_cassette_price` decimal(6,2) DEFAULT NULL,
  
  -- Cable and line prices
  `replace_front_brake_line_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_brake_line_price` decimal(6,2) DEFAULT NULL,
  `replace_front_gear_line_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_gear_line_price` decimal(6,2) DEFAULT NULL,
  
  -- Headset prices
  `repack_headset_price` decimal(6,2) DEFAULT NULL,
  `repack_headset_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Rotor prices
  `replace_front_rotor_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_rotor_price` decimal(6,2) DEFAULT NULL,
  
  -- E-bike and assembly prices
  `ebike_diagnostic_price` decimal(6,2) DEFAULT NULL,
  `bike_assembly_price` decimal(10,2) DEFAULT NULL,
  `ebike_assembly_price` decimal(10,2) DEFAULT NULL,
  
  -- Spoke repair prices (calculated: $35 base + $2 per spoke)
  `front_fix_spoke_price` decimal(10,2) DEFAULT NULL,
  `rear_fix_spoke_price` decimal(10,2) DEFAULT NULL,
  
  -- Custom service price
  `custom_service_price` decimal(10,2) DEFAULT NULL,
  
  -- Total pricing (subtotal and final with 8.75% NYC tax)
  `price` decimal(6,2) DEFAULT NULL,
  `final_price` decimal(10,2) DEFAULT NULL,
  
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
