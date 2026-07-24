# Brooklyn Bikery — Claude Orientation

Bike shop service-tracking app. Customers fill out an intake form on the website, the
shop runs an admin dashboard to log services performed, prices auto-calculate, and an
SMS invoice goes out via Twilio. Single-shop deployment on AWS.

**Live site:** https://brooklynbikery.com
**GitHub repo:** `seanrsn/Spoke` (the repo is named "Spoke" — the local folder is
"Brooklyn Bikery". Don't get confused, they're the same project.)

---

## File map (what lives where, no exceptions)

Everything ships from the repo root. There are no nested src/ folders — Lambdas and
HTML files sit side-by-side. Don't go hunting in subdirs.

### Lambda functions (Python 3.12, all at repo root)

| File | Lambda function name | Purpose |
|---|---|---|
| `Customer-Form.py` | `SubmitCustomerForm` | Public intake form submissions. Creates customer + order rows. |
| `Backend-Form.py` | `SubmitBackendForm` | Admin form submissions. Adds services to existing orders, queues SMS invoice. |
| `Admin-Dashboard.py` | `AdminDashboard` | Admin login (JWT), order search, dashboard data, push notifications. |
| `Send-SMS.py` | `SendSMS` | S3-triggered SMS sender (Twilio) + async push (pywebpush). |

`pymysql` and `pywebpush` get bundled at deploy time (see `.github/workflows/deploy.yml`)
— don't add them to imports without verifying they're in the deploy package layer.

### Static frontend (HTML at repo root)

| File | Page |
|---|---|
| `index.html` | Public homepage (sales / repairs / rentals marketing) |
| `customer-form.html` | Public customer intake form (calls `SubmitCustomerForm`) |
| `backend-form.html` | Admin form for adding services to an order (calls `SubmitBackendForm`) |
| `admin-dashboard.html` | Admin login + order management UI (calls `AdminDashboard`) |
| `privacy.html` / `terms.html` / `error.html` | Static legal/error pages |
| `sw.js` | Service worker for web push notifications |
| `manifest.json` | PWA manifest |

`neonindex.html` is **unused / .gitignored** — old design draft, ignore it.

### Database (multi-tenant, catalog-driven — updated 2026-06)

Migrations in `migrations/` (numbered; staging first, then prod). Key tables:
- `tenants` — one row per shop: slug, display_name, tax_rate, allowed_origin,
  Twilio config, admin_password_secret_arn, invoice_footer, status.
- `customers` — tenant_id, name, phone, date_created, sms_consent(_at),
  sms_opted_out. Phone unique per tenant.
- `orders` — metadata only (bike_description, backend_notes, price,
  final_price). The legacy ~50 service columns were DROPPED (migration 004).
- `order_services` — the line items: order_id → service_catalog_id, quantity,
  price_charged, notes. **Sole source of truth for what was done on an order.**
- `service_catalog` — per-tenant menu: code, display_name, default_price,
  pricing_formula (fixed|spoke|custom), category, is_active, sort_order.
- `messages` — per-tenant SMS history; status flows queued→sent→delivered/
  failed via Twilio status callbacks; twilio_sid recorded.

**Adding/changing a service is a DATA change, not a code change:** the shop
edits it in the dashboard's Services tab (or SQL). The service-entry form,
pricing engine, invoices, and dashboard all render from service_catalog.

### Junk / generated files (don't edit, don't commit)

`*.zip` (lambda packages), `lambda_with_pymysql/`, `sms_tmp/`, `Spoke/` (old GitHub
snapshot), `pywebpush-layer.zip`, `spoke-repo.tar.gz`. All `.gitignore`d.

---

## Deploy flow (auto, GitHub Actions — GATED as of 2026-06)

`.github/workflows/deploy.yml` — auto-runs on push to `main`, `claude/**`, or
`staging`. Serialized via a concurrency group.

1. **`prepare` job**: claude/* pushes auto-merge into main (PRs squash-merge).
2. **`prod-gate` job (prod targets only)**: deploys the exact same code to the
   -staging Lambdas + staging site, then runs `tests/staging_integration.py`
   (9 tests incl. wrong-order regression, tenant isolation, SMS compliance).
   **Prod jobs run only if this is green.**
3. **`deploy-lambdas` / `deploy-frontend` jobs**: package `.py` files → Lambdas;
   sync html/css/js/json → `s3://brooklynbikery.com` + CloudFront invalidation
   (admin pages ship with no-cache). Staging-branch pushes deploy the staging
   stack only and run the same test suite.

**Workflow per the user's preference:** Claude on phone (claude.ai) pushes to
`claude/*` branches → workflow auto-merges + deploys. **Desktop Claude NEVER pushes**
unless explicitly asked, and ALWAYS asks before `git pull`.

---

## Auth model (admin)

- Admin login posts username/password to `AdminDashboard` Lambda.
- Lambda validates against secret in AWS Secrets Manager, issues an HMAC-signed JWT
  (8-hour TTL).
- Rate limit: 5 failed attempts before lockout (in-memory, resets on cold start —
  yes, that's the actual behavior, don't "fix" it without checking with user).
- All admin endpoints check `Authorization: Bearer <jwt>` and verify the HMAC sig.

CORS origin is hardcoded to `https://brooklynbikery.com` via `ALLOWED_ORIGIN` env var.

---

## SMS + push notifications

- `Backend-Form.py` writes a JSON SMS job to S3 → S3 trigger fires `Send-SMS.py`.
- `Send-SMS.py` reads job, calls Twilio REST API, deletes the S3 object on success.
- `Admin-Dashboard.py` invokes `Send-SMS.py` async (`InvocationType=Event`) for web
  push notifications via `pywebpush`. Subscriptions are stored in DynamoDB
  (`push_subscriptions` table — confirm with code before assuming).
- Twilio creds and VAPID keys live in Secrets Manager (`twilio-credentials`).

---

## Common gotchas

- **Don't add stock/inventory logic.** This is a service-tracking app, not e-commerce.
  No products, no SKUs, no carts. (That's MachX.)
- **`*.zip` files at repo root are deploy artifacts** — never edit them by hand.
  They get rebuilt by the workflow.
- **Services live in `service_catalog` + `order_services` (NOT columns).** The
  old boolean columns on `orders` are gone (migration 004). Adding a service =
  a catalog row (dashboard Services tab), zero code changes. Never delete
  catalog rows — deactivate them (`is_active=0`); order history references them.
- **Phone numbers are the customer primary key** (unique constraint). Same phone =
  same customer, even with different names.
- **`Spoke/` folder at root is a stale snapshot** of the old GitHub repo, kept for
  reference but `.gitignore`d. Don't read or modify it.
- **Pricing calculation lives in `Backend-Form.py`** — front/rear distinction matters
  for most services. **Spokes are special:** the formula is `price = 33 + (2 × x)`
  where `x` is the spoke count. 1 spoke = $35, 2 = $37, 3 = $39, etc. The $2 is per
  *additional* spoke, not per spoke. Don't write it as "$35 base + $2 per spoke."

---

## Quick commands

```bash
# Local — none. There's no dev server. Edit HTML, push, deploy verifies in prod.

# AWS — Dommy profile is default, already configured
aws lambda get-function --function-name AdminDashboard --query 'Configuration.LastModified'
aws s3 ls s3://brooklynbikery.com/

# DB — RDS endpoint in Lambda env vars (see AWS console). Usually accessed via SSH
# tunnel through a bastion or by running queries from a Lambda console test.
```

---

## Quick "where is X" cheatsheet

- "Add a new service to the form" → `customer-form.html` or `backend-form.html` (UI),
  `schema.sql` (column), `Backend-Form.py` (insert/price), `Admin-Dashboard.py`
  (search/display).
- "Change SMS template" → `Backend-Form.py`, function that builds the message body.
- "Change admin login behavior" → `Admin-Dashboard.py`, the auth handler.
- "CSS/styling" → inline `<style>` in each HTML file. No separate CSS file.
- "Service worker / push" → `sw.js` + `Admin-Dashboard.py` push subscription handler
  + `Send-SMS.py` push sender.

---

## Staging environment (added 2026-06-02)

There is now a fully isolated **staging** stack in the SAME AWS account (suffix
`-staging`). It mirrors prod's moving parts but **cannot text a real customer or touch
real data**. Spec: `docs/superpowers/specs/2026-06-01-staging-environment-design.md`.
Plan: `docs/superpowers/plans/2026-06-02-staging-environment.md`.

### ⚠️ Working convention — STAGING FIRST
**Every meaningful change deploys to `staging` first, is verified on
`staging.brooklynbikery.com`, THEN merges to `main` for prod.** Prod still auto-deploys
from `main` (fast path unchanged). Push to the `staging` branch to deploy the staging
stack.

### Prod Twilio is ARMED (re-armed 2026-06-03)
Prod SMS is live. Delivery statuses flow back via Twilio status callbacks to
the Admin API and show as ✓/✓✓/✗ in the dashboard. STOP replies auto-record
`customers.sms_opted_out` and Backend-Form refuses invoice texts to opted-out
customers. To disarm in an emergency: remove the S3 → SendSMS notification on
`brooklyn-bikery-sms` (backup of the config: `sms-trigger-backup.json`, local,
gitignored). Ops details: `docs/OPERATIONS.md`. Onboarding a new shop's number:
`docs/A2P-10DLC.md`.

### Staging resource map
| Concern | Prod | Staging |
|---|---|---|
| Lambdas | `SubmitBackendForm` / `AdminDashboard` / `SubmitCustomerForm` / `SendSMS` | same names + `-staging` |
| DB | `bikeshop` | `bikeshop_staging` (same RDS instance) |
| DB secret | `bikeshop-credentials` | `bikeshop-credentials-staging` |
| JWT secret | `bikery-jwt-secret` | `bikery-jwt-secret-staging` (separate; staging tokens can't auth to prod) |
| Admin pw secret | `bikery-admin-password` | `bikery-admin-password-staging` (same password value) |
| Twilio creds | `twilio-credentials` | `twilio-credentials-staging` (TEST creds — placeholder until filled) |
| SMS bucket | `brooklyn-bikery-sms` | `brooklyn-bikery-sms-staging` |
| Push bucket | `brooklyn-bikery-push-jobs` | `brooklyn-bikery-push-jobs-staging` |
| Backend API | `rysf6hggs6` | `dvo3bho9mj` |
| Admin API | `rqshavktfa` | `dm63xxwajj` |
| Customer API | `pp7s8cgqke` | `0nevdp3exi` |
| Frontend bucket | `brooklynbikery.com` | `bikery-staging-site` |
| CloudFront | `E3A6Y3SPOMYKVP` | `E28UATBUDSN4QU` (`dsybtaqd5z3o6.cloudfront.net`) |
| URL | brooklynbikery.com | staging.brooklynbikery.com |

### How isolation works (env-var driven, prod defaults unchanged)
The Lambda code reads `DB_SECRET_ID`, `JWT_SECRET_ID`, `SMS_BUCKET`, `STAGE` from env
vars, **defaulting to the prod values** — so prod behavior is byte-identical with no env
vars set. Staging Lambdas set those vars to the `-staging` resources. Twilio creds and
the admin password are data-driven from the `tenants` row, so pointing staging at
`bikeshop_staging` auto-selects staging Twilio/admin config.

**SMS triple-lock in staging:** separate bucket + Twilio TEST creds + a hard guard in
`Send-SMS.py` (`STAGE=staging` refuses any send whose Twilio account SID is not in
`STAGING_ALLOWED_TWILIO_SIDS`, currently empty → blocks everything until test creds wired).
