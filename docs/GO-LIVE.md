# Pilot Go-Live Checklist

The single source of truth for "what's left before we can sell to another bike
shop." Everything marked ✅ is built and verified on staging. Everything under
**You** needs a human decision or a third party — I can't close those from code.

Last updated: 2026-07-24.

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
  passes (11/11, run in-VPC via the test bridge).
- ✅ **Docs** — ONBOARDING, A2P-10DLC, OPERATIONS runbooks; ToS/Privacy drafts.

## Environment state (IMPORTANT)

Staging is intentionally **ahead of prod** right now. These are on
`staging.brooklynbikery.com` only, NOT in prod, pending an explicit prod push:
- Admin self-service password change (endpoint + dashboard UI)
- Public intake per-shop branding + `?tenant=` slug routing

Prod (`brooklynbikery.com`) has everything from earlier in the readiness work
(catalog, SMS compliance, CORS, export, marketing pages, intake tenant-routing).
To ship the staging-only items to prod: commit the working tree and push
`claude/**` → the gate runs → prod deploys. **Do not push without the owner's
explicit go** (repo rule).

Staging-only scaffolding to clean up before/after a real launch:
- Demo tenant `test-bike-co` (id 3) + its `bikery-admin-password-test-bike-co`
  secret + `test-bike-co.staging.example.com` CORS entries on the 3 staging APIs.
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
