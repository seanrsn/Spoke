-- ============================================================================
-- Migration 003: Create service_catalog + order_services tables, seed Brooklyn
--                Bikery's services
-- ============================================================================
--
-- WHAT THIS DOES
--   Creates two new tables to support per-tenant service curation:
--     - `service_catalog` — per-tenant list of services a shop offers, with
--       default prices and pricing formulas.
--     - `order_services` — line items linking orders to services performed,
--       capturing quantity and the price charged at the time of service.
--   Seeds service_catalog with Brooklyn Bikery's 43 services (40 fixed-price +
--   2 spoke-quantity + 1 custom/ad-hoc), extracted verbatim from
--   Backend-Form.py's `services_with_prices` dict so the prices match exactly.
--
-- IMPACT ON EXISTING WORKFLOW
--   None. The existing Lambdas keep reading/writing to the boolean columns
--   on `orders` and the `price` / `final_price` columns. The new tables sit
--   parallel until step 4 (dual-write) starts populating them on new orders,
--   and step 5 switches Lambdas over to reading from them.
--
-- PRICING FORMULAS
--   `pricing_formula` semantics:
--     - 'fixed' (default): price_charged = default_price * 1 (quantity always 1
--       for fixed services since each service is a single occurrence per order)
--     - 'spoke': price_charged = 33 + 2 * quantity. quantity = spoke count.
--     - 'custom': price_charged = user-input price at order time. default_price
--       is ignored. order_services.notes captures the custom_description.
--
-- BACKFILL HAPPENS IN A SEPARATE SCRIPT
--   `migrations/003b_backfill_order_services.py` walks the 60 existing orders,
--   reads boolean service columns, computes prices, and inserts order_services
--   rows. The backfill is in Python (not SQL) because it needs to recompute
--   spoke prices and verify per-order sum equality against orders.price.
--
-- SAFETY NET
--   RDS snapshot `pre-multitenant-step3-2026-05-18`. 1-click restore available.
-- ============================================================================

USE `bikeshop`;

-- ----------------------------------------------------------------------------
-- 1) service_catalog: per-tenant menu of offered services
-- ----------------------------------------------------------------------------

CREATE TABLE `service_catalog` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL,
  `code` varchar(50) NOT NULL,                       -- stable identifier, e.g. 'front_flat'
  `display_name` varchar(100) NOT NULL,              -- human-readable, e.g. 'Front Flat Repair'
  `default_price` decimal(8,2) DEFAULT NULL,         -- per-unit default; NULL for 'custom'
  `pricing_formula` varchar(20) NOT NULL DEFAULT 'fixed', -- 'fixed' | 'spoke' | 'custom'
  `category` varchar(50) DEFAULT NULL,               -- e.g. 'brakes', 'drivetrain' (for UI grouping)
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `sort_order` int NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `tenant_code` (`tenant_id`, `code`),
  CONSTRAINT `fk_service_catalog_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- 2) order_services: line items for each service performed on an order
-- ----------------------------------------------------------------------------

CREATE TABLE `order_services` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tenant_id` int NOT NULL,
  `order_id` int NOT NULL,
  `service_catalog_id` int NOT NULL,
  `quantity` int NOT NULL DEFAULT '1',
  `price_charged` decimal(8,2) NOT NULL,             -- snapshot of charged price
  `notes` varchar(255) DEFAULT NULL,                 -- e.g. custom service description
  PRIMARY KEY (`id`),
  KEY `tenant_order` (`tenant_id`, `order_id`),
  KEY `service_catalog_id` (`service_catalog_id`),
  CONSTRAINT `fk_order_services_tenant`
    FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `fk_order_services_order`
    FOREIGN KEY (`order_id`) REFERENCES `orders` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_order_services_catalog`
    FOREIGN KEY (`service_catalog_id`) REFERENCES `service_catalog` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ----------------------------------------------------------------------------
-- 3) Seed Brooklyn Bikery's service catalog (tenant_id = 1)
--
-- Prices and codes extracted verbatim from Backend-Form.py:340-381 to ensure
-- the catalog matches the existing pricing logic exactly. Any future price
-- change should update both this seed (for new tenants via provisioning) and
-- the actual catalog row (for Brooklyn Bikery).
-- ----------------------------------------------------------------------------

INSERT INTO `service_catalog`
  (`tenant_id`, `code`, `display_name`, `default_price`, `pricing_formula`, `category`, `sort_order`)
VALUES
  -- Flat tire repairs
  (1, 'front_flat',                'Front Flat Repair',            25.00, 'fixed', 'flats', 1),
  (1, 'rear_flat',                 'Rear Flat Repair',             25.00, 'fixed', 'flats', 2),
  (1, 'front_flat_ebike',          'Front Flat E-Bike',            45.00, 'fixed', 'flats', 3),
  (1, 'rear_flat_ebike',           'Rear Flat E-Bike',             45.00, 'fixed', 'flats', 4),

  -- Brake adjustments
  (1, 'front_brake_adj',           'Front Brake Adj',              20.00, 'fixed', 'brakes', 10),
  (1, 'rear_brake_adj',            'Rear Brake Adj',               20.00, 'fixed', 'brakes', 11),
  (1, 'front_brake_adj_ebike',     'Front Brake Adj E-Bike',       35.00, 'fixed', 'brakes', 12),
  (1, 'rear_brake_adj_ebike',      'Rear Brake Adj E-Bike',        35.00, 'fixed', 'brakes', 13),

  -- Brake pad services
  (1, 'front_replace_vbrake_pads', 'Front Replace V-Brake Pads',   10.00, 'fixed', 'brakes', 20),
  (1, 'rear_replace_vbrake_pads',  'Rear Replace V-Brake Pads',    10.00, 'fixed', 'brakes', 21),
  (1, 'front_new_vbrake_pads',     'Front New V-Brake Pads',       15.00, 'fixed', 'brakes', 22),
  (1, 'rear_new_vbrake_pads',      'Rear New V-Brake Pads',        15.00, 'fixed', 'brakes', 23),
  (1, 'front_replace_disc_pads',   'Front Replace Disc Brake Pads',15.00, 'fixed', 'brakes', 24),
  (1, 'rear_replace_disc_pads',    'Rear Replace Disc Brake Pads', 15.00, 'fixed', 'brakes', 25),
  (1, 'front_new_disc_pads',       'Front New Disc Brake Pads',    20.00, 'fixed', 'brakes', 26),
  (1, 'rear_new_disc_pads',        'Rear New Disc Brake Pads',     20.00, 'fixed', 'brakes', 27),
  (1, 'front_hydraulic_brake_bleed','Front Hydraulic Brake Bleed', 50.00, 'fixed', 'brakes', 28),
  (1, 'rear_hydraulic_brake_bleed','Rear Hydraulic Brake Bleed',   50.00, 'fixed', 'brakes', 29),

  -- Derailleur / shifting
  (1, 'front_derailleur_adj',      'Front Derailleur Adj',         20.00, 'fixed', 'drivetrain', 40),
  (1, 'rear_derailleur_adj',       'Rear Derailleur Adj',          20.00, 'fixed', 'drivetrain', 41),

  -- Tune-up
  (1, 'tune_up',                   'Tune-Up',                     100.00, 'fixed', 'service', 50),

  -- Drivetrain
  (1, 'replace_cassette',          'Replace Cassette/Freewheel',   15.00, 'fixed', 'drivetrain', 60),
  (1, 'new_bb',                    'New Bottom Bracket',           45.00, 'fixed', 'drivetrain', 61),
  (1, 'replace_chain',             'Replace Chain',                15.00, 'fixed', 'drivetrain', 62),
  (1, 'replace_crank_bb',          'Replace Crank/BB',             30.00, 'fixed', 'drivetrain', 63),

  -- Cables and lines
  (1, 'replace_front_brake_line',  'Replace Front Brake Line',     25.00, 'fixed', 'cables', 70),
  (1, 'replace_rear_brake_line',   'Replace Rear Brake Line',      25.00, 'fixed', 'cables', 71),
  (1, 'replace_front_gear_line',   'Replace Front Gear Line',      25.00, 'fixed', 'cables', 72),
  (1, 'replace_rear_gear_line',    'Replace Rear Gear Line',       25.00, 'fixed', 'cables', 73),

  -- Wheel services
  (1, 'front_wheel_true',          'Front Wheel Truing',           20.00, 'fixed', 'wheels', 80),
  (1, 'rear_wheel_true',           'Rear Wheel Truing',            20.00, 'fixed', 'wheels', 81),
  (1, 'replace_front_rotor',       'Replace Front Rotor',          15.00, 'fixed', 'wheels', 82),
  (1, 'replace_rear_rotor',        'Replace Rear Rotor',           15.00, 'fixed', 'wheels', 83),
  (1, 'front_repack_wheel',        'Front Repack Wheel',           25.00, 'fixed', 'wheels', 84),
  (1, 'rear_repack_wheel',         'Rear Repack Wheel',            25.00, 'fixed', 'wheels', 85),

  -- Headset
  (1, 'repack_headset',            'Repack Headset',               25.00, 'fixed', 'headset', 90),
  (1, 'repack_headset_ebike',      'Repack Headset E-Bike',        35.00, 'fixed', 'headset', 91),

  -- E-bike and assembly
  (1, 'ebike_diagnostic',          'E-Bike Diagnostic',            40.00, 'fixed', 'ebike', 100),
  (1, 'bike_assembly',             'Bike Assembly',               100.00, 'fixed', 'assembly', 101),
  (1, 'ebike_assembly',            'E-Bike Assembly',             150.00, 'fixed', 'assembly', 102),

  -- Spokes (quantity-based; price = 33 + 2*qty)
  (1, 'front_fix_spoke',           'Front Fix Spoke',              35.00, 'spoke', 'wheels', 110),
  (1, 'rear_fix_spoke',            'Rear Fix Spoke',               35.00, 'spoke', 'wheels', 111),

  -- Custom (ad-hoc; price set per order, default_price is informational only)
  (1, 'custom_service',            'Custom Service',                NULL, 'custom', 'custom', 120);

-- ----------------------------------------------------------------------------
-- 4) Verify
-- ----------------------------------------------------------------------------
-- Expect 43 rows for tenant 1, with 40 'fixed', 2 'spoke', 1 'custom'.
--
--   SELECT pricing_formula, COUNT(*) FROM service_catalog
--   WHERE tenant_id = 1 GROUP BY pricing_formula;
--
-- Expected:
--   fixed:  40
--   spoke:  2
--   custom: 1
-- ============================================================================
