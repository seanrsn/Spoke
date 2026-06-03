# Staging Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan phase-by-phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an isolated `-staging` stack (same AWS account) that can never text real customers or mutate real order data, so all future changes are tested on `staging.brooklynbikery.com` before reaching prod.

**Architecture:** Duplicate the 4 Lambdas with `-staging` suffix; point them (via env vars) at a separate `bikeshop_staging` database on the existing RDS instance, a separate `bikery-jwt-secret-staging`, and a separate `brooklyn-bikery-sms-staging` bucket. Twilio test credentials + a hard `STAGE` guard make SMS delivery impossible. Separate S3+CloudFront serve `staging.brooklynbikery.com`. Prod deploy stays as-is; a new `staging` branch deploys staging.

**Tech Stack:** AWS Lambda (Python 3.12), API Gateway, RDS MySQL, S3, CloudFront, ACM, Route53, Secrets Manager, Twilio (test creds), GitHub Actions, boto3.

**Reference spec:** `docs/superpowers/specs/2026-06-01-staging-environment-design.md`

**Conventions for execution:**
- AWS profile `Dommy` (default), region `us-east-1`.
- Verify after every resource creation; never assume.
- Prod Lambdas are NOT redeployed by this plan. They keep running current code (functionally identical to the parametrized code, since env-var defaults equal today's hardcoded values). Parametrized code reaches prod only on the next normal `main` deploy.
- Use boto3 over AWS CLI on Windows (cp1252/emoji + path-mangling issues). Encode log output `.encode('ascii','replace')`.

---

## Phase 0: Safety baseline (verify, don't change)

- [ ] **Step 0.1: Re-confirm prod Twilio is disarmed and SMS queue empty**

Run (boto3): bucket notification config on `brooklyn-bikery-sms` has 0 LambdaFunctionConfigurations; `sms/` prefix has no >0-byte objects.
Expected: 0 configs, queue empty.

- [ ] **Step 0.2: Snapshot current prod Lambda configs for later comparison**

For each of `SubmitBackendForm`, `AdminDashboard`, `SubmitCustomerForm`, `SendSMS`: record `Runtime`, `Role`, `Handler`, `Timeout`, `MemorySize`, `Environment.Variables`, `VpcConfig`, `Layers`. Save to `staging-build/prod-lambda-configs.json` (gitignored).
Expected: JSON written; note which Lambdas are in a VPC (SendSMS is non-VPC per prior work).

---

## Phase 1: Prod-safe code parametrization (repo change, prod-identical defaults)

**Files:** Modify `Backend-Form.py`, `Admin-Dashboard.py`, `Customer-Form.py`, `Send-SMS.py`.

Pattern: replace hardcoded secret IDs / bucket names with `os.getenv("VAR", "<current value>")` so prod (no env var set) is byte-identical.

- [ ] **Step 1.1: Backend-Form.py — DB + JWT secret IDs + SMS bucket**

- `get_db_secret()` (line ~146): `SecretId="bikeshop-credentials"` → `SecretId=os.getenv("DB_SECRET_ID", "bikeshop-credentials")`.
- `get_jwt_secret()` (line ~65): `SecretId="bikery-jwt-secret"` → `SecretId=os.getenv("JWT_SECRET_ID", "bikery-jwt-secret")`.
- SMS job write (line ~711, `sms_bucket = 'brooklyn-bikery-sms'` or inline): use `os.getenv("SMS_BUCKET", "brooklyn-bikery-sms")`.

- [ ] **Step 1.2: Admin-Dashboard.py — DB + JWT secret IDs + SMS bucket**

- `get_secret()` (line 50): `SecretId="bikeshop-credentials"` → `os.getenv("DB_SECRET_ID", "bikeshop-credentials")`.
- `get_jwt_secret()` (line 125): `secret_id = "bikery-jwt-secret"` → `secret_id = os.getenv("JWT_SECRET_ID", "bikery-jwt-secret")`.
- `/send-sms` handler (line 876): `sms_bucket = 'brooklyn-bikery-sms'` → `os.getenv("SMS_BUCKET", "brooklyn-bikery-sms")`.
- `PUSH_BUCKET` (line 511) already parametrized — leave as is.

- [ ] **Step 1.3: Customer-Form.py — DB secret ID**

- Line 54: `SecretId="bikeshop-credentials"` → `os.getenv("DB_SECRET_ID", "bikeshop-credentials")`.

- [ ] **Step 1.4: Send-SMS.py — hard STAGE guard**

Add near config (after line ~31): `STAGE = os.environ.get("STAGE", "prod")`.
In `send_sms()` after resolving `account_sid` (line ~116), insert guard:

```python
# Staging can NEVER reach a real handset. Refuse any non-test Twilio account.
if STAGE == "staging" and not account_sid.startswith("AC0000000000000000000000000000"):
    # Twilio test Account SIDs are the magic test SID; live SIDs differ.
    print(f"BLOCKED: STAGE=staging refusing live Twilio send (sid prefix {account_sid[:8]})")
    return {"ok": False, "blocked": True, "reason": "staging guard"}
```

(Test-credential Account SID is provided by the user; replace the literal prefix check with the actual test SID once known — see Phase 6. Until then the guard blocks ALL sends when STAGE=staging, which is the safe default.)

- [ ] **Step 1.5: Verify prod-identical + syntax**

Run `py_compile` on all 4 files. Confirm: with no env vars set, every `os.getenv` returns the original literal. Grep each file to confirm no stray hardcoded `"bikeshop-credentials"` / `"bikery-jwt-secret"` / `'brooklyn-bikery-sms'` remain at the changed call sites.
Expected: all compile; defaults equal originals.

- [ ] **Step 1.6: Commit (local; do NOT push yet)**

```bash
git add Backend-Form.py Admin-Dashboard.py Customer-Form.py Send-SMS.py
git commit -m "refactor: parametrize secret IDs / SMS bucket / STAGE via env vars (prod-safe defaults)"
```

---

## Phase 2: Staging database (`bikeshop_staging`)

- [ ] **Step 2.1: Create the database**

Connect to RDS with prod creds (`bikeshop-credentials`), run `CREATE DATABASE IF NOT EXISTS bikeshop_staging CHARACTER SET utf8mb4;`.
Verify: `SHOW DATABASES LIKE 'bikeshop_staging'` returns 1 row.

- [ ] **Step 2.2: Replicate schema (structure only, no prod rows)**

Dump prod schema structure and apply to staging:
`mysqldump --no-data --single-transaction bikeshop <all tables> | mysql bikeshop_staging` — or, if mysqldump unavailable on Windows, replicate via boto3/pymysql: for each table in prod `bikeshop`, run `SHOW CREATE TABLE`, then execute the CREATE against `bikeshop_staging`. Tables: tenants, customers, orders, service_catalog, order_services, messages, push_subscriptions (+ any others present).
Verify: `SHOW TABLES` in staging matches prod table list.

- [ ] **Step 2.3: Copy config rows (catalog + tenant) with staging overrides**

- Copy all `service_catalog` rows from prod → staging (verbatim).
- Copy the `tenants` row (id=1) → staging, then UPDATE the staging copy:
  - `allowed_origin = 'https://staging.brooklynbikery.com'`
  - `twilio_account_sid = '<TEST account SID>'` (Phase 6; placeholder until provided)
  - `twilio_auth_token_secret_arn = '<staging twilio test secret>'` (Phase 3)
  - `admin_password_secret_arn = '<staging admin password secret>'` (Phase 3)
  - `twilio_from_number` = a Twilio magic test number (e.g. `+15005550006`) or left as prod value (guard blocks send anyway)
  - keep `tax_rate`, `display_name`, `sms_sender_name`, `slug`, `status='active'`.
Verify: staging `tenants` row present with overridden fields; `service_catalog` count matches prod.

- [ ] **Step 2.4: Seed synthetic customers + orders**

Insert ~3 fake customers (e.g. names `Test Rider 1/2/3`, phones `+15005550001..3` — Twilio magic test numbers) and 1–2 orders each with a few `order_services` line items referencing real catalog ids.
Verify: counts > 0; confirm NO real customer phone (e.g. Paul/George numbers) exists in staging — `SELECT COUNT(*) FROM customers WHERE phone IN (<real numbers>)` = 0.

---

## Phase 3: Staging secrets

- [ ] **Step 3.1: `bikeshop-credentials-staging`**

Create secret with the SAME host/user/password as prod but `"database":"bikeshop_staging"`.
Verify: retrievable; `database` field == `bikeshop_staging`.

- [ ] **Step 3.2: `bikery-jwt-secret-staging`**

Create secret `{"secret":"<random 48+ char>"}` (distinct from prod). Generate randomness from a non-forbidden source (e.g. `secrets.token_urlsafe(48)` in a normal python process — NOT Math.random in workflow scripts).
Verify: retrievable; `secret` non-empty; differs from prod JWT secret.

- [ ] **Step 3.3: Staging admin password secret**

Create secret holding the staging admin password (value may equal prod password for convenience). Match the format `get_secret_value(SecretId=admin_password_secret_arn)` expects (inspect prod admin password secret shape first, replicate it).
Verify: retrievable; shape matches prod admin secret.

- [ ] **Step 3.4: Staging Twilio test-token secret (PLACEHOLDER → user provides)**

Create secret matching the shape `_resolve_twilio_creds` expects: `{"auth_token":"<TEST token>", "account_sid":"<TEST sid>", "from_number":"<magic test number>"}` (confirm exact keys by inspecting prod `twilio-credentials`/`twilio_auth_token_secret_arn` secret shape).
**FLAG:** Test SID/token come from the Twilio console (Account → Keys & Tokens → Test Credentials). Create the secret with placeholder values now; fill real values when user provides. Guard in Step 1.4 keeps staging safe meanwhile.
Verify: secret exists with correct keys.

---

## Phase 4: Staging Lambdas (4×)

- [ ] **Step 4.1: Package current (parametrized) code**

Build a deploy zip per Lambda from the Phase-1 code, bundling `pymysql` (and `pywebpush` for SendSMS) exactly as `deploy.yml` does.
Verify: zips built, contain `lambda_function.py`.

- [ ] **Step 4.2: Create each `-staging` Lambda cloning prod config**

For each of (SubmitBackendForm, AdminDashboard, SubmitCustomerForm, SendSMS): `create-function` named `<Prod>-staging` using the same Role/Runtime/Handler/Timeout/Memory/VpcConfig/Layers captured in Step 0.2, with the parametrized zip.
Env vars per staging Lambda:
- All: `DB_SECRET_ID=bikeshop-credentials-staging`, `ALLOWED_ORIGIN=https://staging.brooklynbikery.com`
- Backend/Admin: also `JWT_SECRET_ID=bikery-jwt-secret-staging`, `SMS_BUCKET=brooklyn-bikery-sms-staging`
- Admin: also `PUSH_BUCKET=brooklyn-bikery-push-jobs-staging` (Phase 6 optional) or keep prod push bucket (push isn't a customer-text risk) — default: keep prod push bucket to reduce scope.
- SendSMS: `STAGE=staging`, `SMS_BUCKET=brooklyn-bikery-sms-staging`
Verify: each function ACTIVE; `get-function-configuration` shows correct env vars; LastUpdateStatus Successful.

- [ ] **Step 4.3: Smoke-invoke AdminDashboard-staging (read-only)**

Invoke with an unauthenticated request → expect 401 (proves it runs and connects). Then mint a staging JWT (using `bikery-jwt-secret-staging`) and hit a read endpoint → expect 200 with staging data only.
Verify: 401 then 200; data returned is the synthetic seed, never real customers.

---

## Phase 5: Staging API Gateway

- [ ] **Step 5.1: Create staging API routes**

Mirror prod's API routes to the `-staging` Lambdas. Simplest: a new HTTP API (or a `staging` stage) with the same routes (`/submitservices`, admin routes, `/send-sms`, customer submit, Twilio inbound webhook). Add `lambda add-permission` for API Gateway → each staging Lambda.
Verify: each route returns expected status (OPTIONS→CORS 200/204; protected→401 without token).

- [ ] **Step 5.2: Record staging API base URLs**

Save the staging invoke URLs (services API, admin API, customer API). These feed the staging frontend build (Phase 7) and the deploy pipeline (Phase 8).
Verify: URLs reachable.

---

## Phase 6: Staging SMS pipeline (triple-locked)

- [ ] **Step 6.1: Create bucket `brooklyn-bikery-sms-staging`**

Create the bucket (private). Verify: exists, not public.

- [ ] **Step 6.2: Wire ObjectCreated(prefix `sms/`) → SendSMS-staging**

Add `lambda add-permission` (s3.amazonaws.com, source = staging bucket ARN) then put bucket notification config (ObjectCreated:*, prefix `sms/`) targeting `SendSMS-staging`.
Verify: notification config present on staging bucket; prod `brooklyn-bikery-sms` config UNCHANGED (still 0 configs).

- [ ] **Step 6.3: Provide Twilio test creds (FLAG → user)**

Update the Step 3.4 secret + staging `tenants` row with real test SID/token/from-number. Update the Step 1.4 guard literal to match the real test Account SID prefix, redeploy SendSMS-staging.
**FLAG:** blocked on user-provided test creds. Until provided, leave guard blocking all sends (staging SMS is inert-but-safe).

- [ ] **Step 6.4: Prove SMS cannot deliver**

Drop a test `sms/` job in the staging bucket addressed to a magic test number. Confirm SendSMS-staging runs and either (a) blocked by STAGE guard, or (b) accepted by Twilio test creds with NO real delivery. Confirm prod SendSMS did NOT run (check prod logs).
Verify: no real SMS sent; prod SendSMS untouched.

---

## Phase 7: Staging frontend (S3 + ACM + CloudFront + Route53)

- [ ] **Step 7.1: Create staging static bucket**

Create S3 bucket for staging assets (e.g. `staging.brooklynbikery.com` or `bikery-staging-site`). Configure for CloudFront OAC/OAI (match prod bucket's access model).
Verify: bucket exists.

- [ ] **Step 7.2: Request ACM cert (us-east-1) for staging.brooklynbikery.com**

`request-certificate` (DNS validation). Read the validation CNAME, add it to Route53 zone `Z086515138135RWJ9AA9K`. Wait for `ISSUED`.
Verify: cert status ISSUED.

- [ ] **Step 7.3: Create CloudFront distribution**

Origin = staging bucket, alternate domain `staging.brooklynbikery.com`, the ACM cert, default root `index.html`, error mapping like prod. Mirror prod cache behavior EXCEPT: admin-dashboard.html / backend-form.html served `no-cache` (carry over the Phase-8 deploy cache rules).
Verify: distribution Deployed; default domain reachable over HTTPS.

- [ ] **Step 7.4: Route53 ALIAS staging.brooklynbikery.com → CloudFront**

Add A/AAAA ALIAS records to the CloudFront dist.
Verify: `staging.brooklynbikery.com` resolves and serves over HTTPS.

- [ ] **Step 7.5: Build + upload staging frontend (staging API URLs)**

Copy the repo HTML, rewrite prod API base URLs → staging API URLs (Phase 5), upload to the staging bucket with the same cache rules as prod deploy (admin pages `no-cache`). Invalidate the staging CloudFront.
Verify: `https://staging.brooklynbikery.com/admin-dashboard.html` loads; login works against staging API; a test order edit hits `bikeshop_staging` only.

---

## Phase 8: Deploy pipeline (`staging` branch target)

**Files:** Modify `.github/workflows/deploy.yml`.

- [ ] **Step 8.1: Add a target selector keyed on branch**

`main`/`claude/**`/PR → prod (current behavior, unchanged). `staging` branch → staging target: deploy to `-staging` Lambdas, sync to staging S3 bucket, invalidate staging CloudFront, and rewrite API URLs to staging. Keep the prepare-job auto-merge logic for `claude/**`→main intact.
Show the exact YAML diff: a `TARGET` derived from `github.ref`, a function-name map per target, bucket/distribution vars per target, and the API-URL rewrite step for staging only.

- [ ] **Step 8.2: Verify staging deploy in isolation**

Push the `staging` branch. Confirm: only `-staging` Lambdas + staging bucket/CDN updated; prod Lambdas' LastModified UNCHANGED; prod bucket objects UNCHANGED.
Verify: prod untouched; staging updated.

---

## Phase 9: Convention + docs

**Files:** Modify `CLAUDE.md`.

- [ ] **Step 9.1: Document the staging-first rule + resource map**

Add a "Staging environment" section: the staging-first convention ("every meaningful change → `staging` branch → verify on staging.brooklynbikery.com → then `main`"), the full staging resource map (Lambdas, DB, buckets, secrets, URL, API), the Twilio test-cred + STAGE guard, and the note that prod Twilio is currently disarmed until explicitly re-armed. Also correct the stale schema description (CLAUDE.md still describes the pre-migration boolean-column orders schema).
Verify: section reads correctly; resource names match what was built.

- [ ] **Step 9.2: Commit docs + deploy.yml**

```bash
git add CLAUDE.md .github/workflows/deploy.yml docs/superpowers/
git commit -m "docs+ci: add staging environment, staging-first convention, staging deploy target"
```

---

## Phase 10: End-to-end verification

- [ ] **Step 10.1: Staging smoke test (full workflow)**

On staging: create a customer/order, run an "Edit Services" with explicit orderId (wrong-order fix), confirm the right order is edited in `bikeshop_staging`, queue an SMS invoice and confirm it is BLOCKED/undelivered, log in to the admin dashboard.
Verify: all steps work on staging; no real delivery.

- [ ] **Step 10.2: Prod-untouched audit**

Confirm: prod Lambda LastModified unchanged by staging work; prod `bikeshop` data unchanged; prod `brooklyn-bikery-sms` still has 0 trigger configs (Twilio still disarmed); Paul's order 73 still $195/5 items.
Verify: prod fully intact.

- [ ] **Step 10.3: Report + next-step decision**

Summarize what's live, hand back to user. Twilio prod re-arm (task #2) remains user-gated.

---

## Self-review notes

- **Spec coverage:** all 5 isolation boundaries (compute=P4, data=P2, SMS=P1.4/P6, frontend=P7, secrets=P1/P3) + deploy model (P8) + convention/docs (P9) covered.
- **External dependency / FLAG:** Twilio test credentials (Step 3.4 / 6.3) — only true blocker; everything else proceeds without it (guard keeps staging safe).
- **Prod-safety:** prod Lambdas not redeployed here; env-var defaults equal current values; prod bucket notification untouched; verified in P10.2.
