# Multi-tenancy Steps 8–10 Implementation Plan

> **For agentic workers:** Execute on the STAGING stack first (never prod). Gate completion on the isolation test (Task 4). Promote to prod only after staging is green AND the user approves.

**Goal:** Make the app serve multiple shops securely. A request's tenant is derived from the authenticated user (carried in the JWT); every data query is scoped by `tenant_id`. Then onboard tenants via a script and prove isolation with a real second tenant.

**Chosen mechanism (decided):** Tenant-in-JWT. Login carries a shop slug (absent → Brooklyn Bikery / tenant 1, so BB is unchanged); login validates against that tenant's password and stamps `tenant_id` into the JWT; every endpoint reads `tenant_id` from the verified token (absent → 1). Forward-compatible with the planned central-login site `bluewrenchhq.com`.

**Failure mode to guard:** a query missing `WHERE tenant_id = %s` leaks one shop's data to another. The isolation test (Task 4) mechanically catches this.

**Safety:** BB is tenant 1; adding `WHERE tenant_id=1` changes nothing for it. All dev/test on staging.

---

## Task 1: Auth layer — tenant in the JWT (Admin-Dashboard.py)

**Files:** Modify `Admin-Dashboard.py`.

- [ ] **1.1** Add a slug→id resolver:
```python
def resolve_tenant_id(payload=None, body=None):
    """Tenant for this request. Prefer the verified JWT claim; else a login slug;
    else default to Brooklyn Bikery (1) so existing single-tenant traffic is unchanged."""
    if payload and payload.get("tenant_id"):
        return int(payload["tenant_id"])
    slug = (body or {}).get("tenant") or (body or {}).get("shop")
    if slug:
        secret = get_secret()
        conn = pymysql.connect(host=secret["host"], user=secret["user"], password=secret["password"],
                               database=secret["database"], connect_timeout=5)
        try:
            with conn.cursor() as c:
                c.execute("SELECT id FROM tenants WHERE slug=%s AND status='active'", (slug,))
                row = c.fetchone()
                if row: return int(row[0])
        finally:
            conn.close()
    return 1
```
- [ ] **1.2** `get_admin_password(tenant_id=1)` — take a tenant id, `get_tenant(tenant_id)`.
- [ ] **1.3** Login handler: `tid = resolve_tenant_id(body=body)`; `get_admin_password(tid)`; on success `create_jwt({"role":"admin","ip":ip_address,"tenant_id":tid}, jwt_secret)`.
- [ ] **1.4** Verify: minting a JWT without `tenant_id` and login without a slug both resolve to 1 (BB unchanged).

## Task 2: Scope every query by tenant_id (Admin-Dashboard.py + Backend-Form.py)

For each authenticated handler, compute `tid = resolve_tenant_id(payload=payload)` and add `tenant_id = %s` (= `tid`) to the WHERE/INSERT. **Audit list (Admin-Dashboard.py):**

- [ ] **2.1** get-db-tables (line ~1030/1039): `SELECT * FROM customers WHERE tenant_id=%s`; `... FROM orders WHERE tenant_id=%s`.
- [ ] **2.2** search orders (line ~1423): add `AND o.tenant_id=%s`.
- [ ] **2.3** order_services merge (line ~243, ~1473): add `AND tenant_id=%s` (already partly scoped — verify).
- [ ] **2.4** messaging list/threads (lines ~1117, 1127, 1188-1197): add `WHERE tenant_id=%s`.
- [ ] **2.5** customer lookups (lines ~541, ~1363): add `AND tenant_id=%s`.
- [ ] **2.6** push_subscriptions (lines ~534, ~1322): add `tenant_id=%s` (push table has tenant_id).
- [ ] **2.7** message INSERTs (lines ~793, ~918): already include tenant_id — set it to `tid` not hardcoded 1.
- [ ] **2.8** Backend-Form.py: customer/order lookups in the new-customer + edit branches — scope by `tid` (the explicit-orderId path already does `WHERE id=%s AND tenant_id=%s`; the legacy phone-lookup + customer create must use `tid`). `get_tenant(1)` → `get_tenant(tid)`. `order_services` writes already use `TENANT_ID`; make that `tid`.
- [ ] **2.9** Customer-Form.py (public, retired): if revived, tenant must come from the request context; for now leave defaulting to 1 but note it.
- [ ] **2.10** Grep sweep: `FROM (customers|orders|order_services|messages|push_subscriptions)` across all Lambdas → confirm NONE lack a tenant filter.

## Task 3: provision_tenant.py (step 9)

**Files:** Create `provision_tenant.py` (+ optional `migrations/`-style helper).

- [ ] **3.1** CLI: `python provision_tenant.py --slug acme --name "Acme Bikes" --db <prod|staging>`.
- [ ] **3.2** Creates: a Secrets Manager admin-password secret (random or provided); inserts a `tenants` row (slug, display_name, tax_rate default 0.0875, allowed_origin, twilio fields, admin_password_secret_arn, status=active); seeds that tenant's `service_catalog` by copying tenant 1's catalog with the new tenant_id.
- [ ] **3.3** Idempotent; prints the new tenant_id + login slug. Refuses to clobber an existing slug.

## Task 4: Isolation test + staging proof (step 10) — THE GATE

**Files:** Extend `tests/staging_integration.py`.

- [ ] **4.1** Add `test_tenant_isolation`: provision a throwaway `test-shop` tenant in `bikeshop_staging` (via provision_tenant.py against staging), create a customer+order under it, then assert:
  - logged in as BB (tenant 1) via get-db-tables → sees ONLY tenant-1 rows, NOT the test-shop order;
  - logged in as test-shop → sees ONLY test-shop rows;
  - a BB-scoped order edit cannot touch a test-shop order (404/refused).
  - Teardown: delete test-shop data + tenant + secret.
- [ ] **4.2** Run full suite on staging. The existing 4 tests must still pass (BB/tenant-1 unaffected). New isolation test must pass. If isolation fails → a query is unscoped → fix Task 2 and re-run.

## Task 5: Deploy to staging + iterate

- [ ] **5.1** Package + deploy the changed Lambdas to the `-staging` functions (boto3, as in the staging build).
- [ ] **5.2** Run `tests/staging_integration.py` until 5/5 green.
- [ ] **5.3** Commit locally on `claude/staging-environment` (or a new branch). Do NOT push (prod deploy) without explicit user approval.

## Task 6: Promote to prod (USER-GATED)

- [ ] **6.1** Only after staging is green + user approves: the parametrized + tenant-scoped code goes to prod via the normal main deploy (BB stays tenant 1, behavior unchanged). Re-run a prod smoke check (BB login + a read) to confirm no regression.

---

## Self-review
- **Spec coverage:** step 8 = Tasks 1+2; step 9 = Task 3; step 10 = Task 4. Promotion = Task 6.
- **Risk:** unscoped query → cross-tenant leak. Mitigation: Task 2.10 grep sweep + Task 4 isolation test gate.
- **Prod safety:** all defaults resolve to tenant 1; BB unchanged; staging-first; prod promotion user-gated.
