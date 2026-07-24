# Operations Runbook (BlueWrench / Brooklyn Bikery platform)

Account 807373873973, us-east-1. CLI runs as IAM user `Dommy`.

## Monitoring & alerts

**CloudWatch alarms (9)** exist for: each prod Lambda's Errors, each prod
API's 5xx, RDS free storage < 2 GB, RDS CPU > 90% (15 min). They currently
have **no notification actions** because the `Dommy` IAM user cannot create
SNS topics.

### ➤ ONE-TIME OWNER ACTION: unlock email alerts
From the AWS **console as root/admin** (not the CLI):
1. IAM → Users → Dommy → Add permissions → attach `AmazonSNSFullAccess`.
2. Then tell Claude "wire up the alarm emails" — or run:
```bash
aws sns create-topic --name bikery-ops-alerts
aws sns subscribe --topic-arn <TopicArn> --protocol email --notification-endpoint supergalaxyguy007@gmail.com
for A in prod-AdminDashboard-errors prod-SubmitBackendForm-errors prod-SubmitCustomerForm-errors prod-SendSMS-errors prod-admin-api-5xx prod-backend-api-5xx prod-customer-api-5xx prod-rds-low-storage prod-rds-high-cpu; do
  aws cloudwatch put-metric-alarm --alarm-name "$A" ... --alarm-actions <TopicArn>   # (re-put with same params + action)
done
```
(Confirm the subscription email when it arrives.)

## Database

- Instance `brooklyn-bikery` (db.t4g.micro, 20 GB, encrypted).
- **Backups: 14 days** automated retention. **Deletion protection: ON.**
- Databases: `bikeshop` (prod), `bikeshop_staging` (staging) — same instance.
- Point-in-time restore: RDS console → Restore to point in time → creates a
  NEW instance → update the `host` in `bikeshop-credentials` secret → Lambdas
  pick it up on next cold start. Practice target: < 30 min.
- Migrations live in `migrations/`, numbered. Apply staging first, then prod.
  Additive migrations before code deploys; destructive only after code stops
  referencing (see 004 as the model).

## Deploys

- **Everything goes through the gate.** Any push that targets prod
  (`main`, `claude/**`, PRs) first deploys the same code to the -staging
  stack and runs `tests/staging_integration.py` (9 tests). Prod jobs run only
  if the gate is green. Pushes to `staging` branch deploy staging only.
- Pipeline is serialized (`concurrency: deploy-pipeline`).
- Rollback: `git revert` the bad commit and push — the revert goes through
  the same gate. For emergencies, Lambda code can be rolled back by
  deploying the previous git version of the single `.py` file.

## SMS pipeline

- Backend-Form / Admin-Dashboard write jobs to `s3://brooklyn-bikery-sms/sms/`
  → S3 event → SendSMS → Twilio. Failed sends move to `failed/` with the
  reason in object metadata. Check: `aws s3 ls s3://brooklyn-bikery-sms/failed/`.
- Delivery status: Twilio posts callbacks to the Admin API
  (`?msgRowId=`) which updates `messages.status`
  (queued → sent → delivered/failed). Dashboard shows ✓ / ✓✓ / ✗.
- STOP/START from customers auto-recorded on `customers.sms_opted_out`;
  Backend-Form refuses invoice texts to opted-out customers (`sms:"optout"`).
- Staging can never text a human: separate bucket + Twilio TEST creds +
  `STAGE=staging` hard SID allowlist in Send-SMS.py.
- Re-arm/disarm prod SMS: the S3→SendSMS notification on
  `brooklyn-bikery-sms`; backup of the config in `sms-trigger-backup.json`
  (local, gitignored).

## Secrets map

| Secret | Purpose |
|---|---|
| bikeshop-credentials[-staging] | DB creds (host swap here = DB failover) |
| bikery-jwt-secret[-staging] | JWT signing (separate per env) |
| bikery-admin-password[-staging] | BB admin login |
| bikery-admin-password-<slug> | other shops' logins (created by provisioning) |
| twilio-credentials[-staging] | BB / test Twilio auth tokens |
| bikery-vapid-keys | web push |

**Password reset for a shop:** `aws secretsmanager put-secret-value
--secret-id bikery-admin-password-<slug> --secret-string '{"password":"<new>"}'`
then tell the owner. (No self-service reset yet — known gap.)

## Per-shop domains (when bluewrenchhq.com is purchased)

1. Route53 hosted zone for bluewrenchhq.com; ACM cert for
   `*.bluewrenchhq.com` (us-east-1).
2. Add the wildcard as an alternate domain on the prod CloudFront
   distribution (E3A6Y3SPOMYKVP) + Route53 A-alias records per shop.
3. Frontend + backend already resolve tenants from `{slug}.bluewrenchhq.com`
   hostnames; provisioning already registers each shop's origin for CORS.
   No code changes needed.

## Known gaps (accepted for now)

- Login rate-limit is in-memory per Lambda container (resets on cold start).
- One shared password per shop; no per-staff users/roles/audit trail.
- No billing automation (manual Stripe).
- No IaC — infra is hand-built; this file + CLAUDE.md are the source of truth.
- Alarm emails blocked on the SNS permission (top of this file).
