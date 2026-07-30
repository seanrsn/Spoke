-- 007: make spoke (quantity-based) pricing data-driven instead of hardcoded.
--
-- Spoke repairs were priced by a hardcoded formula in Backend-Form.py
-- (33 + 2*qty). That meant no shop could set its own spoke pricing. We now
-- store the two parameters per catalog row:
--   default_price          = price of the FIRST spoke   (Brooklyn Bikery: $35)
--   additional_unit_price  = price of EACH ADDITIONAL   (Brooklyn Bikery: $2)
-- Spoke line total = default_price + additional_unit_price * (qty - 1)
--   qty 1 -> 35, qty 2 -> 37, qty 3 -> 39  (identical to the old 33 + 2*qty)
--
-- Additive + nullable: fixed/custom rows ignore the column; code falls back to
-- the historical 35/2 if it's null, so this is safe to apply before the code.

ALTER TABLE service_catalog
  ADD COLUMN additional_unit_price DECIMAL(10,2) NULL
  AFTER default_price;

-- Seed every existing spoke row with Brooklyn Bikery's historical $2/additional.
UPDATE service_catalog
   SET additional_unit_price = 2.00
 WHERE pricing_formula = 'spoke';

-- Ensure the first-spoke price is set (should already be 35 for all spoke rows).
UPDATE service_catalog
   SET default_price = 35.00
 WHERE pricing_formula = 'spoke'
   AND (default_price IS NULL OR default_price = 0);
