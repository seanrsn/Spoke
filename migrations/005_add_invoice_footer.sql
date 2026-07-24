-- ============================================================================
-- Migration 005: Per-tenant invoice footer
-- ============================================================================
--
-- WHAT THIS DOES
--   Adds `tenants.invoice_footer` — the free-text block rendered at the bottom
--   of every SMS invoice (shop hours, payment methods, sign-off). Previously
--   this text was hardcoded in Backend-Form.py's build_invoice_text() and was
--   Brooklyn Bikery-specific, which would have leaked BB's hours and payment
--   methods onto every other shop's invoices.
--
-- BEHAVIOR CONTRACT
--   - Brooklyn Bikery (tenant 1) gets its EXACT current footer text, so BB
--     invoices are byte-identical before/after the code switch.
--   - Tenants with NULL footer fall back to a neutral "Thank you!" line
--     (DEFAULT_INVOICE_FOOTER in Backend-Form.py). A new shop must never
--     inherit another shop's hours.
--   - The trailing "Reply STOP to unsubscribe" line is NOT part of the footer;
--     the code always appends it (compliance line, not branding).
--
-- APPLY ORDER
--   Run this BEFORE deploying the Backend-Form.py that selects invoice_footer.
--   Additive + idempotent-safe to re-run the UPDATEs; the ALTER fails cleanly
--   if the column already exists.
-- ============================================================================

ALTER TABLE `tenants` ADD COLUMN `invoice_footer` TEXT NULL;

-- Brooklyn Bikery: verbatim copy of the previously hardcoded footer block.
UPDATE `tenants` SET `invoice_footer` =
'⏰ Hours:
Mon: Closed
Tue: Closed
Wed: 6:30-8:30 PM
Thu-Fri: 10 AM-6 PM
Sat-Sun: 10 AM-4 PM

💳 Payment Methods:
• Zelle, Venmo, CashApp, Cash
• Credit cards (+2.9% fee)

Thank you! 🙏'
WHERE `id` = 1;
