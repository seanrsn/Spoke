# Staging Environment — Design Spec

**Date:** 2026-06-01
**Status:** Approved (pending written-spec review)
**Priority:** Ultra-critical
**Author:** Sean + Claude

---

## 1. Problem & motivation

Brooklyn Bikery (repo: `seanrsn/Spoke`) runs a single production AWS stack with **no
isolated place to develop or test**. Two recent incidents corrupted **real** customer
data and sent a **real** buggy SMS:

- A wrong-order bug edited the wrong customer's order (George #72, Paul #73).
- During investigation, the only way to be sure testing couldn't text a customer was to
  *physically disconnect Twilio* (remove the S3 → SendSMS notification on
  `brooklyn-bikery-sms`). Twilio is currently disarmed and will stay disarmed until this
  environment exists.

Paul is not only a customer — he is a **prospective client for the multi-tenant platform**
this app is becoming. A buggy message reaching him is a trust/sales problem, not just a bug.

**The two failure modes this environment must make structurally impossible during testing:**
1. Sending an SMS to a real person.
2. Reading or mutating real customer/order data.

## 2. Goals

- A complete, separate **staging** copy of the app's moving parts that cannot reach prod
  data or real phones, even when the code under test is broken.
- Keep the existing **production** deploy flow fast and unchanged.
- Make "test on staging first" the default working convention, recorded durably.
- Low cost (~$2–5/month), no second RDS instance, reversible.

## 3. Non-goals (explicitly out of scope for this spec)

- Multi-tenancy migration step 8 (tenant resolution from URL) — separate task #3.
- Messaging delivery-status feedback in UI/DB — separate task #4.
- A second AWS account (considered and rejected: more setup + a 2nd RDS bill for
  marginal extra isolation at this stage).
- A hard CI gate blocking prod (user chose convention-based "staging first", not a gate).

## 4. Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Isolation level | Staging stack in the **same AWS account**, `-staging` suffix | Strong code/data/SMS isolation, cheap, moderate setup |
| Database | Separate **`bikeshop_staging`** DB on the **existing** RDS instance | No 2nd RDS cost; same engine/schema |
| Staging data | **Synthetic seed** (fake customers/orders) + copied `service_catalog` & `tenants` config | No real PII in staging; predictable tests |
| Twilio | **Real Twilio TEST credentials** in staging | Exercises the real SMS code path; API validates but never delivers |
| Frontend URL | **`staging.brooklynbikery.com`** | `brooklynbikery.com` is in Route53 (zone `Z086515138135RWJ9AA9K`) → cert + DNS scriptable |
| Promotion | **main → prod auto-deploy stays as-is**; new `staging` branch → staging stack | User preference; fast prod flow preserved |
| Working convention | **Every meaningful change deploys to `staging` first, is verified, then merges to `main`.** Recorded in CLAUDE.md | The behavioral half of "never again" |

## 5. Architecture — five isolation boundaries

### 5.1 Compute (duplicate Lambdas)
Four staging Lambdas, identical code to prod, distinguished only by env vars:

| Prod | Staging |
|---|---|
| `SubmitBackendForm` | `SubmitBackendForm-staging` |
| `AdminDashboard` | `AdminDashboard-staging` |
| `SubmitCustomerForm` | `SubmitCustomerForm-staging` |
| `SendSMS` | `SendSMS-staging` |

Each staging Lambda gets its own API Gateway route/stage. Prod Lambdas/API are never
modified by a staging deploy.

### 5.2 Data (separate database, shared instance)
- `CREATE DATABASE bikeshop_staging` on the existing RDS instance.
- Apply the same schema/migrations as prod.
- Seed: a handful of synthetic customers + orders; copy real `service_catalog` and
  `tenants` rows so pricing/branding match prod.
- Staging Lambdas connect only to `bikeshop_staging` (via a staging DB secret). No code
  path from staging to the prod `bikeshop` database.
- **Accepted risk:** shared RDS instance means a runaway staging query could affect prod
  instance performance. Acceptable at current scale; revisit if load grows.

### 5.3 SMS (triple-guaranteed: cannot reach a human) 🔒
Three independent safeguards, any one of which is sufficient:
1. **Separate bucket** `brooklyn-bikery-sms-staging` with its own ObjectCreated → `SendSMS-staging`
   notification. *(Essential: if staging wrote to the prod `brooklyn-bikery-sms` bucket,
   the PROD SendSMS would pick it up and send a real text. Separate bucket closes that hole.)*
2. **Twilio TEST credentials** in `SendSMS-staging` — Twilio validates the request but
   never delivers to a real handset (test magic numbers only).
3. **Hard env guard** in `SendSMS-staging`: when `STAGE=staging`, refuse to call live
   Twilio regardless of which creds are present.

### 5.4 Frontend (separate bucket + URL + CDN)
- New S3 bucket for staging static assets.
- New CloudFront distribution + ACM cert (us-east-1) for `staging.brooklynbikery.com`,
  DNS-validated via the existing Route53 zone; ALIAS record added in Route53.
- Admin runs over HTTPS (required for secure context / service worker).
- **Fallback:** if cert/DNS validation stalls, ship first on the CloudFront default
  `*.cloudfront.net` domain and attach the subdomain afterward — does not block the rest.

### 5.5 Secrets (parametrized; prod stays byte-identical)
The **only** change touching prod code. Lambdas currently hardcode secret IDs
(`bikeshop-credentials`, `bikery-jwt-secret`, etc.). Change to read from env vars with
the **current value as the default**, e.g.:

```python
DB_SECRET_ID = os.getenv("DB_SECRET_ID", "bikeshop-credentials")
```

- Prod Lambdas set no new env var → behavior is identical to today.
- Staging Lambdas override env vars to point at staging secrets:
  `bikeshop-credentials-staging` (DB), staging Twilio test creds, a staging admin
  password, and (optionally) a staging JWT secret.

New staging secrets to create: `bikeshop-credentials-staging` (DB), staging Twilio test
creds, staging admin password, and a **separate** `bikery-jwt-secret-staging`. The JWT
secret is deliberately *not* shared with prod, so a staging-issued admin token cannot
authenticate against the production dashboard.

## 6. Deploy / promotion model

- `main` → existing `deploy.yml` jobs deploy to the **prod** stack, unchanged.
- New `staging` branch → same workflow, parametrized to target the **`-staging`** Lambdas,
  staging S3 bucket, and staging CloudFront invalidation.
- `deploy.yml` gains a target selector keyed off the branch (`main` = prod, `staging` =
  staging). Lambda function-name map and bucket/distribution IDs are chosen per target.
- **Convention (recorded in CLAUDE.md):** meaningful changes go to `staging` first →
  verified on `staging.brooklynbikery.com` → then merged to `main` for prod.

## 7. Cost

~$2–5/month: no new RDS (shared instance), one small S3 bucket, one low-traffic
CloudFront distribution (largely within free tier at this volume), a few Secrets Manager
secrets (~$0.40 each/month), negligible extra Lambda invocations.

## 8. Reversibility

Every staging resource is suffixed/separate and can be deleted without touching prod:
drop `bikeshop_staging`, delete the `-staging` Lambdas, staging buckets, staging
CloudFront/cert, staging secrets, and the `staging` branch. The prod-code change (env-var
secret IDs with prod defaults) is inert for prod and can stay regardless.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Staging accidentally points at prod DB/bucket | Separate secret + separate bucket; staging never references prod IDs. Verify env vars post-deploy. |
| A staging SMS reaches a real phone | Triple guard (separate bucket + test creds + hard env guard). |
| Shared RDS instance contention | Accepted at current scale; monitor; revisit if load grows. |
| Cert/DNS validation delay blocks launch | Fallback to `*.cloudfront.net` domain, attach subdomain later. |
| Prod code change has unintended effect | Env-var defaults equal current hardcoded values → prod path unchanged; verify with a diff/no-op deploy. |

## 10. Post-build follow-ups (not part of this spec)

- Re-arm prod Twilio (restore S3 notification from `sms-trigger-backup.json`, purge test
  jobs first) — task #2, only after staging is verified and user approves.
- Multi-tenancy step 8 — task #3.
- Messaging delivery-status feedback — task #4.
