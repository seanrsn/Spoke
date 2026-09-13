# Pilot Go-Live Checklist

The single source of truth for "what's left before we can sell to another bike
shop." Everything marked ✅ is built and verified on staging. Everything under
**You** needs a human decision or a third party — I can't close those from code.

Last updated: 2026-09-07.

---

## Engineering readiness — DONE & verified on staging

- ✅ **Multi-tenant onboarding, proven end-to-end.** A fresh shop provisioned
  from scratch (`provision_tenant.py`) logs in with its own credentials, gets
  its own service catalog + tax rate, edits its own prices, takes public intake
  under its own tenant (consent recorded), logs services at its own pricing, and
  is fully isolated from every other shop. Dry-run: 7/7.
- ✅ **Data-driven catalog** — services/prices are per-shop data, editable in the
  dashboard; no code change to add a service.
- ✅ **Per-shop branding** — login, service-entry, dashboard, AND the public
  intake page all show the shop's own name (verified: a second shop shows its
  name; Brooklyn Bikery unchanged).
- ✅ **SMS compliance** — consent captured at intake, STOP/START honored and
  recorded, invoice texts refused to opted-out customers, Twilio delivery status
  (✓/✓✓/✗) tracked.
- ✅ **Self-service password change** for a logged-in admin.
- ✅ **Per-tenant CSV export** (customers / orders / messages).
- ✅ **Gated deploys** — prod deploys only after the staging integration suite
  passes (15/15, run in-VPC via the test bridge).
- ✅ **Docs** — ONBOARDING, A2P-10DLC, OPERATIONS runbooks; ToS/Privacy drafts.

## Environment state (IMPORTANT)

Prod and `main` have been in sync since 2026-07-30 (the readiness-hardening
branch auto-merged and all four Lambdas + the site deployed that day). The
self-service password change and branded intake ARE in prod.

The **`staging` branch is ahead of prod** with the pre-pilot hardening
(2026-09-07). On `staging.brooklynbikery.com` only, pending an explicit prod
push:
- **Per-shop Twilio webhook validation.** Inbound texts and delivery-status
  callbacks are validated with the token of the shop they're for (resolved
  from the `?msgRowId=` message row, else the shop's number), so a shop on its
  own Twilio subaccount works, and a shop with no token yet is refused (403)
  instead of being validated — and filed — under Brooklyn Bikery. Status
  updates and push-unsubscribe are tenant-scoped.
- **Fail-closed tenant resolution.** Unknown login slug → 401; token without
  a tenant claim → 401 (admin + backend); public intake with an unknown
  `?tenant=` slug → 400, from an unrecognized Origin → 403. Nothing falls
  through to tenant 1 unless the request is genuinely for the shared host.
- **Tenant config cache TTL (5 min)** so Twilio/status edits take effect on
  warm containers without a redeploy (`TENANT_CACHE_TTL` env overrides).
- **Migration 008** (drop the `DEFAULT '1'` bridge on `tenant_id`) — **applied
  to staging 2026-09-12** (suite 14/14 afterwards), **not yet applied to prod**.
  Staging command, for the record:
  `BIKERY_DB_SECRET=bikeshop-credentials-staging python migrations/run_migration.py migrations/008_drop_tenant_id_defaults.sql`
  then prod (no env var). The code does not depend on it either way; it makes
  a future unscoped INSERT fail loudly instead of filing under tenant 1.
- Suite is 15 tests (was 12): + `test_webhook_per_tenant_validation`,
  `test_fail_closed`.

- **Customers-tab fix (2026-09-13).** `get-db-tables` 500'd whenever any
  customer had `sms_consent_at` set; prod has 3 such customers, so the prod
  Customers tab is broken until this is promoted. Regression test
  `test_customers_tab_with_consented_customer` added.

**Promote this before the first real shop** (and soon regardless — it
carries the prod Customers-tab fix). To promote: merge `staging`
into `main` (or push a `claude/**` branch) → the prod gate runs the suite on
staging → prod deploys. **Do not push to prod without the owner's explicit
go** (repo rule).

Staging-only scaffolding to clean up before/after a real launch:
- Demo tenant `test-bike-co` (id 3) + its `bikery-admin-password-test-bike-co`
  secret + `test-bike-co.staging.example.com` CORS entries on the 3 staging APIs.
  (`test-shop`, id 2, is REQUIRED by the isolation tests — keep it on staging.)
- `TENANT_ORIGIN_TTL=2` env on `SubmitCustomerForm-staging` (test-only; prod uses 300).

---

## Punch list — YOU (or a third party)

| # | Item | Owner | Action | Blocker? |
|---|------|-------|--------|----------|
| 1 | **Legal** | Lawyer | Have counsel review `bluewrench-terms.html` + `bluewrench-privacy.html`; remove the "draft" banner when cleared. | Yes for public sale |
| 2 | **A2P 10DLC** | You + shop | Register each shop's SMS per `docs/A2P-10DLC.md` (needs their legal name/EIN). ~1–2 wk carrier lead. App works meanwhile; texting waits. | Yes for that shop's SMS |
| 3 | **Pricing** | You | Confirm or replace placeholder $59 / $119 / $299 on `bluewrench-pricing.html`. | No (cosmetic until first sale) |
| 4 | **Alert emails** | You (AWS admin) | Grant the IAM user SNS access, then I wire CloudWatch alarms → email (~10 min). Steps in `docs/OPERATIONS.md`. | No |
| 5 | **Domain** | You | Buy `bluewrenchhq.com` for per-shop subdomains (`{shop}.bluewrenchhq.com`). Code already resolves tenants by hostname. | No (works today via `?tenant=`) |
| 6 | **Billing** | You | Decide billing (manual Stripe invoice is fine for pilots; automation later). | No for pilots |

## Remaining optional code item (not a pilot blocker)

- **Password self-reset for a locked-out admin.** The self-service *change*
  (knowing the current password) is done. True forgot-password recovery needs a
  design decision — where the reset code is sent (a verified admin phone/email
  we don't yet store per tenant) — so it's deliberately not built. White-glove
  reset is a one-command op today (`OPERATIONS.md`), which covers pilots.

---

## Bottom line

For onboarding a first pilot shop **white-glove**, the platform is
engineering-ready and verified. The gate to a *public, self-serve* sale is
items #1–#2 (legal + SMS registration), which are external by nature.
