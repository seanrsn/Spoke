#!/usr/bin/env python3
"""
Staging integration tests for Brooklyn Bikery.

Runs against the ISOLATED staging stack (never prod). Safe to run anytime: it only
touches bikeshop_staging, uses Twilio TEST creds (cannot deliver), and cleans up its
own throwaway data.

The centerpiece is `test_wrong_order_regression` — it reproduces the exact conditions
that corrupted real customers' orders (editing "the latest order for a phone") and
asserts the explicit-orderId fix targets the right order and refuses on mismatch. If
this ever fails, DO NOT promote to prod.

Usage:
    python tests/staging_integration.py
Requires: boto3, pymysql, and AWS credentials with access to the staging stack
(the GitHub Actions staging deploy job has these). Exit code 0 = all passed.

NOTE: staging resource IDs are pinned here. If the staging stack is rebuilt with new
API IDs, update STAGING below (also referenced in deploy.yml's staging frontend rewrite).
"""
import os, sys, json, time, base64, hmac, hashlib
import urllib.request, urllib.error
import boto3, pymysql

REGION = "us-east-1"
STAGING = {
    "admin_api":   "https://dm63xxwajj.execute-api.us-east-1.amazonaws.com/stage",
    "admin_fn":    "AdminDashboard-staging",
    "backend_fn":  "SubmitBackendForm-staging",
    "customer_fn": "SubmitCustomerForm-staging",
    "sendsms_fn":  "SendSMS-staging",
    "db_secret":   "bikeshop-credentials-staging",
    "admin_pw_secret": "bikery-admin-password-staging",
    "jwt_secret":  "bikery-jwt-secret-staging",
    # The Twilio account SIDs are read from these secrets at runtime — never
    # hardcoded, so the prod live SID does not live in the repo.
    "twilio_secret":      "twilio-credentials-staging",
    "live_twilio_secret": "twilio-credentials",
}

sm  = boto3.client("secretsmanager", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)

def _secret(name): return json.loads(sm.get_secret_value(SecretId=name)["SecretString"])

# ── DB access via the in-VPC bridge ────────────────────────────────────────
# GitHub-hosted runners cannot reach RDS (the DB security group is
# IP-allowlisted and runner IPs are dynamic), so the suite routes SQL through
# StagingTestRunner-staging — a Lambda in the app's VPC that runs each query
# against bikeshop_staging and refuses any non-staging schema. These shims
# make that bridge look exactly like the pymysql DictCursor the tests expect,
# so every test body is unchanged. When RDS is directly reachable (e.g. a dev
# machine on the allowlist) set BIKERY_DIRECT_DB=1 to bypass the bridge.
STAGING.setdefault("db_bridge_fn", "StagingTestRunner-staging")
_DIRECT_DB = os.environ.get("BIKERY_DIRECT_DB") == "1"

class _BridgeCursor:
    def __init__(self): self._rows = []; self._i = 0; self.rowcount = -1; self.lastrowid = None
    def execute(self, sql, params=None):
        r = lam.invoke(FunctionName=STAGING["db_bridge_fn"],
                       Payload=json.dumps({"query": sql, "params": list(params) if params else []}).encode())
        out = json.loads(r["Payload"].read())
        if not isinstance(out, dict) or not out.get("ok"):
            raise RuntimeError(f"bridge query failed: {out.get('error') if isinstance(out, dict) else out}")
        self._rows = out.get("rows") or []; self._i = 0
        self.rowcount = out.get("rowcount", -1); self.lastrowid = out.get("lastrowid")
        return self.rowcount
    def fetchone(self):
        if self._i >= len(self._rows): return None
        row = self._rows[self._i]; self._i += 1; return row
    def fetchall(self):
        rest = self._rows[self._i:]; self._i = len(self._rows); return rest
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

class _BridgeConn:
    def cursor(self): return _BridgeCursor()
    def commit(self): pass      # bridge autocommits each statement
    def rollback(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _db():
    if _DIRECT_DB:
        c = _secret(STAGING["db_secret"])
        assert c["database"] == "bikeshop_staging", f"SAFETY: db secret points at {c['database']}, not staging!"
        return pymysql.connect(host=c["host"], user=c["user"], password=c["password"],
                               database=c["database"], charset="utf8mb4",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=True, connect_timeout=10)
    return _BridgeConn()

def _mint_jwt():
    secret = _secret(STAGING["jwt_secret"])["secret"]
    def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": "staging-integration-test", "exp": int(time.time()) + 600}).encode())
    s = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"

def _http(url, body, token=None):
    """POST to the admin 'API' by invoking the Lambda directly.

    Direct invocation (instead of the public execute-api URL) makes the suite
    runnable from ANY network position with AWS credentials: a developer
    machine, GitHub's runners, or the in-VPC StagingTestRunner Lambda (whose
    subnet has AWS-service endpoints but no NAT, so public URLs hang). The
    handler sees the same API-Gateway-v2-shaped event either way.
    """
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    event = {
        "version": "2.0",
        "rawPath": "/stage/AdminDashboard",
        "requestContext": {"http": {"method": "POST", "path": "/stage/AdminDashboard"},
                            "domainName": "dm63xxwajj.execute-api.us-east-1.amazonaws.com"},
        "headers": headers,
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }
    r = lam.invoke(FunctionName=STAGING["admin_fn"], Payload=json.dumps(event).encode())
    out = json.loads(r["Payload"].read())
    try:
        parsed = json.loads(out.get("body") or "{}")
    except Exception:
        parsed = {}
    return out.get("statusCode"), parsed


def _invoke_admin_webhook(form, signature, row_id):
    """Deliver a Twilio-style webhook (form-encoded) straight to the Lambda,
    shaped exactly like API Gateway would deliver it — incl. the fields the
    signature validator uses to reconstruct the public URL."""
    import urllib.parse as _up
    event = {
        "version": "2.0",
        "rawPath": "/stage/AdminDashboard",
        "rawQueryString": f"msgRowId={row_id}" if row_id is not None else "",
        "queryStringParameters": ({"msgRowId": str(row_id)} if row_id is not None else None),
        "requestContext": {"http": {"method": "POST", "path": "/stage/AdminDashboard"},
                            "domainName": "dm63xxwajj.execute-api.us-east-1.amazonaws.com"},
        "headers": {"content-type": "application/x-www-form-urlencoded",
                     "x-forwarded-proto": "https",
                     "x-twilio-signature": signature},
        "body": _up.urlencode(form),
        "isBase64Encoded": False,
    }
    r = lam.invoke(FunctionName=STAGING["admin_fn"], Payload=json.dumps(event).encode())
    out = json.loads(r["Payload"].read())
    return out.get("statusCode")

def _invoke_backend(payload):
    event = {"httpMethod": "POST", "requestContext": {"http": {"method": "POST"}},
             "headers": {"Authorization": f"Bearer {_mint_jwt()}"}, "body": json.dumps(payload)}
    r = lam.invoke(FunctionName=STAGING["backend_fn"], Payload=json.dumps(event).encode())
    out = json.loads(r["Payload"].read())
    return out.get("statusCode"), out

# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────
def test_login_and_auth():
    """Unauthed admin request is rejected; correct password yields a JWT."""
    sc, _ = _http(STAGING["admin_api"] + "/AdminDashboard", {"action": "get-db-tables"})
    assert sc == 401, f"unauth should be 401, got {sc}"
    pw = _secret(STAGING["admin_pw_secret"])["password"]
    sc, b = _http(STAGING["admin_api"] + "/AdminDashboard", {"action": "login", "password": pw})
    assert sc == 200 and b.get("token"), f"login should return 200+token, got {sc} {list(b)}"
    return "login 200 + token; unauth 401"

def test_data_isolation():
    """Admin API returns the staging dataset, and the tenant is the staging tenant."""
    pw = _secret(STAGING["admin_pw_secret"])["password"]
    _, b = _http(STAGING["admin_api"] + "/AdminDashboard", {"action": "login", "password": pw})
    sc, data = _http(STAGING["admin_api"] + "/AdminDashboard", {"action": "get-db-tables"}, b["token"])
    assert sc == 200, f"get-db-tables {sc}"
    with _db() as conn, conn.cursor() as c:
        c.execute("SELECT slug FROM tenants WHERE id=1")
        slug = c.fetchone()["slug"]
    assert slug == "brooklyn-bikery-staging", f"tenant slug is {slug!r}, expected staging tenant"
    return f"served staging data; tenant slug={slug}"

def test_wrong_order_regression():
    """THE regression test for the order-corruption bug. Edit must hit the EXACT order
    (by orderId), never 'the most recent order for a phone', and refuse on phone mismatch."""
    conn = _db()
    A_PHONE, B_PHONE = "+19995550001", "+19995550002"
    cidA = cidB = oidA = oidB = None
    try:
        with conn.cursor() as c:
            # Self-heal residue from an interrupted prior run — but ONLY rows
            # that carry this test's own signature (ITEST_* names on the
            # reserved +1999555 phones). Anything else on these phones is
            # unexpected real data: abort rather than clobber.
            c.execute("SELECT id, name FROM customers WHERE phone IN (%s,%s)", (A_PHONE, B_PHONE))
            leftovers = c.fetchall()
            foreign = [r for r in leftovers if not (r["name"] or "").startswith("ITEST_")]
            if foreign:
                raise AssertionError(f"non-test rows on reserved test phones: {foreign}; aborting to avoid clobber")
            for r in leftovers:
                c.execute("DELETE FROM order_services WHERE order_id IN (SELECT id FROM orders WHERE customer_id=%s)", (r["id"],))
                c.execute("DELETE FROM orders WHERE customer_id=%s", (r["id"],))
                c.execute("DELETE FROM customers WHERE id=%s", (r["id"],))
            if leftovers:
                print(f"    (cleaned {len(leftovers)} ITEST_ residue row(s) from an interrupted run)")
            today = time.strftime("%Y-%m-%d")
            c.execute("INSERT INTO customers (name,phone,date_created) VALUES ('ITEST_A',%s,%s)", (A_PHONE, today)); cidA = c.lastrowid
            c.execute("INSERT INTO customers (name,phone,date_created) VALUES ('ITEST_B',%s,%s)", (B_PHONE, today)); cidB = c.lastrowid
            c.execute("INSERT INTO orders (tenant_id,customer_id,date_of_service,backend_notes) VALUES (1,%s,%s,'A0')", (cidA, today)); oidA = c.lastrowid
            c.execute("INSERT INTO orders (tenant_id,customer_id,date_of_service,backend_notes) VALUES (1,%s,%s,'B0')", (cidB, today)); oidB = c.lastrowid  # newest

        def notes(oid):
            with conn.cursor() as c:
                c.execute("SELECT backend_notes FROM orders WHERE id=%s", (oid,)); return c.fetchone()["backend_notes"]

        # 1) happy path: orderId=A + matching phone -> edits A only
        sc, _ = _invoke_backend({"isNewCustomer": False, "orderId": oidA, "lookupPhone": A_PHONE,
                                 "services": [], "notes": "A_EDITED"})
        assert sc == 200, f"happy path expected 200, got {sc}"
        assert notes(oidA) == "A_EDITED", "order A was not edited"
        assert notes(oidB) == "B0", "order B (newest) was wrongly touched!"

        # 2) mismatch guard: orderId=A + phone B -> 409, nothing changes
        sc, _ = _invoke_backend({"isNewCustomer": False, "orderId": oidA, "lookupPhone": B_PHONE,
                                 "services": [], "notes": "SHOULD_NOT_APPLY"})
        assert sc == 409, f"mismatch should be 409, got {sc}"
        assert notes(oidA) == "A_EDITED" and notes(oidB) == "B0", "data mutated despite 409"

        # 3) unknown order -> 404
        sc, _ = _invoke_backend({"isNewCustomer": False, "orderId": 999999999, "lookupPhone": A_PHONE,
                                 "services": [], "notes": "X"})
        assert sc == 404, f"unknown order should be 404, got {sc}"
        return "explicit orderId targets the right order; 409 on mismatch; 404 on unknown"
    finally:
        with conn.cursor() as c:
            if oidA and oidB: c.execute("DELETE FROM order_services WHERE order_id IN (%s,%s)", (oidA, oidB))
            c.execute("DELETE FROM messages WHERE phone IN (%s,%s) OR to_number IN (%s,%s)", (A_PHONE, B_PHONE, A_PHONE, B_PHONE))
            if oidA and oidB: c.execute("DELETE FROM orders WHERE id IN (%s,%s)", (oidA, oidB))
            if cidA and cidB: c.execute("DELETE FROM customers WHERE id IN (%s,%s)", (cidA, cidB))
        # purge any SMS jobs the edit queued (cannot deliver anyway — test creds)
        s3 = boto3.client("s3", REGION)
        for o in s3.list_objects_v2(Bucket="brooklyn-bikery-sms-staging", Prefix="sms/").get("Contents", []):
            s3.delete_object(Bucket="brooklyn-bikery-sms-staging", Key=o["Key"])
        conn.close()

def test_sms_cannot_deliver():
    """Config guarantee: staging can never send a real SMS — test SID only, live SID never allowlisted."""
    test_sid = _secret(STAGING["twilio_secret"])["account_sid"]
    live_sid = _secret(STAGING["live_twilio_secret"])["account_sid"]
    with _db() as conn, conn.cursor() as c:
        c.execute("SELECT twilio_account_sid FROM tenants WHERE id=1")
        sid = c.fetchone()["twilio_account_sid"]
    assert sid == test_sid, f"staging tenant SID is {sid}, expected the TEST sid"
    env = lam.get_function_configuration(FunctionName=STAGING["sendsms_fn"])["Environment"]["Variables"]
    assert env.get("STAGE") == "staging", "SendSMS-staging STAGE != staging"
    allow = {s.strip() for s in env.get("STAGING_ALLOWED_TWILIO_SIDS", "").split(",") if s.strip()}
    assert live_sid not in allow, "LIVE Twilio SID is allowlisted in staging — DANGER"
    assert allow == {test_sid}, f"allowlist should be exactly the test SID, got {allow}"
    return "STAGE=staging; allowlist is exactly the test SID; live SID NOT allowlisted"

def test_tenant_isolation():
    """Step 10 — the multi-tenant GATE. A second tenant (test-shop) must be fully
    isolated from Brooklyn Bikery (tenant 1): no cross-tenant reads, no cross-tenant
    edits. Requires `test-shop` provisioned on staging (provision_tenant.py)."""
    admin = STAGING["admin_api"] + "/AdminDashboard"
    conn = _db()
    with conn.cursor() as c:
        c.execute("SELECT id FROM tenants WHERE slug = 'test-shop'")
        row = c.fetchone()
        assert row, "test-shop not provisioned — run: python provision_tenant.py --slug test-shop --db staging"
        t2 = row["id"]
    t2_pw = _secret("bikery-admin-password-test-shop")["password"]
    bb_pw = _secret(STAGING["admin_pw_secret"])["password"]
    MARK = "ISO_TEST_SHOP_ORDER"
    cid2 = oid2 = None
    try:
        # Create a customer + order under test-shop (tenant 2)
        with conn.cursor() as c:
            today = time.strftime("%Y-%m-%d")
            c.execute("INSERT INTO customers (tenant_id,name,phone,date_created) VALUES (%s,'ISO Rider','+19995551234',%s)", (t2, today)); cid2 = c.lastrowid
            c.execute("INSERT INTO orders (tenant_id,customer_id,date_of_service,backend_notes) VALUES (%s,%s,%s,%s)", (t2, cid2, today, MARK)); oid2 = c.lastrowid

        # Logged in as Brooklyn Bikery (tenant 1) — must NOT see test-shop's order
        _, b = _http(admin, {"action": "login", "password": bb_pw}); bb_tok = b["token"]
        _, data = _http(admin, {"action": "get-db-tables"}, bb_tok)
        bb_order_ids = [r["id"] for r in data["orders"]["rows"]]
        bb_notes = [r.get("backend_notes") for r in data["orders"]["rows"]]
        assert oid2 not in bb_order_ids, "LEAK: Brooklyn Bikery can see test-shop's order id!"
        assert MARK not in bb_notes, "LEAK: test-shop order content visible to Brooklyn Bikery!"

        # Logged in as test-shop — must see ONLY its own order, never BB's
        _, b = _http(admin, {"action": "login", "tenant": "test-shop", "password": t2_pw})
        assert b.get("token"), "test-shop login failed (slug-based tenant login broken)"
        _, data = _http(admin, {"action": "get-db-tables"}, b["token"])
        t2_order_ids = [r["id"] for r in data["orders"]["rows"]]
        assert oid2 in t2_order_ids, "test-shop cannot see its own order"
        with conn.cursor() as c:
            c.execute("SELECT id FROM orders WHERE tenant_id = 1 LIMIT 10")
            bb_ids = {r["id"] for r in c.fetchall()}
        assert not (bb_ids & set(t2_order_ids)), "LEAK: test-shop can see Brooklyn Bikery's orders!"

        # A Brooklyn-Bikery-scoped edit of the test-shop order must be refused (404)
        sc, _ = _invoke_backend({"isNewCustomer": False, "orderId": oid2, "services": [], "notes": "CROSS_TENANT_HACK"})
        assert sc == 404, f"cross-tenant edit should be 404, got {sc}"
        with conn.cursor() as c:
            c.execute("SELECT backend_notes FROM orders WHERE id = %s", (oid2,))
            assert c.fetchone()["backend_notes"] == MARK, "cross-tenant edit MUTATED test-shop data!"
        return f"tenant {t2} fully isolated from Brooklyn Bikery — no cross-tenant read or edit"
    finally:
        with conn.cursor() as c:
            if oid2:
                c.execute("DELETE FROM order_services WHERE order_id = %s", (oid2,))
                c.execute("DELETE FROM orders WHERE id = %s", (oid2,))
            if cid2:
                c.execute("DELETE FROM customers WHERE id = %s", (cid2,))
        conn.close()

def test_login_returns_shop():
    """Login returns the shop's display name (powers the header), correct per tenant."""
    admin = STAGING["admin_api"] + "/AdminDashboard"
    bb_pw = _secret(STAGING["admin_pw_secret"])["password"]
    _, b = _http(admin, {"action": "login", "password": bb_pw})
    assert b.get("shop") == "Brooklyn Bikery [STAGING]", f"BB shop name wrong: {b.get('shop')!r}"
    t2_pw = _secret("bikery-admin-password-test-shop")["password"]
    _, b2 = _http(admin, {"action": "login", "tenant": "test-shop", "password": t2_pw})
    assert b2.get("shop") == "Test Shop", f"test-shop name wrong: {b2.get('shop')!r}"
    return f"shop names correct: BB='{b['shop']}', test-shop='{b2['shop']}'"

def test_new_customer_flow():
    """The New-Customer create path (tenant-scoped INSERTs from step 8) creates a
    customer + order + service line item under the right tenant. Validates the
    INSERT INTO customers/orders (tenant_id, ...) changes end-to-end via the Lambda."""
    conn = _db()
    PHONE = "+19995557777"
    cid = oid = None
    try:
        with conn.cursor() as c:  # ensure clean slate
            c.execute("DELETE os FROM order_services os JOIN orders o ON o.id=os.order_id JOIN customers cu ON cu.id=o.customer_id WHERE cu.phone=%s", (PHONE,))
            c.execute("DELETE o FROM orders o JOIN customers cu ON cu.id=o.customer_id WHERE cu.phone=%s", (PHONE,))
            c.execute("DELETE FROM customers WHERE phone=%s", (PHONE,))
        # Create as tenant 1 (minted JWT with no tenant_id -> defaults to 1)
        st, _ = _invoke_backend({"isNewCustomer": True, "name": "ZZ NewCust Test", "phone": PHONE,
                                 "services": ["Replace Chain ($15)"], "notes": "newcust", "bikeDescription": "test"})
        assert st == 200, f"new-customer create expected 200, got {st}"
        with conn.cursor() as c:
            c.execute("SELECT id, tenant_id FROM customers WHERE phone=%s", (PHONE,))
            crow = c.fetchone(); assert crow, "customer not created"; cid = crow["id"]
            assert crow["tenant_id"] == 1, f"customer tenant_id={crow['tenant_id']}, expected 1"
            c.execute("SELECT id, tenant_id, price FROM orders WHERE customer_id=%s", (cid,))
            orow = c.fetchone(); assert orow, "order not created"; oid = orow["id"]
            assert orow["tenant_id"] == 1, f"order tenant_id={orow['tenant_id']}, expected 1"
            assert float(orow["price"]) == 15.0, f"price={orow['price']}, expected 15"
            c.execute("SELECT COUNT(*) n FROM order_services WHERE order_id=%s AND tenant_id=1", (oid,))
            assert c.fetchone()["n"] == 1, "service line item not written under tenant 1"
        return "new-customer create -> customer+order+service under tenant 1, price $15"
    finally:
        with conn.cursor() as c:
            if oid:
                c.execute("DELETE FROM order_services WHERE order_id=%s", (oid,))
                c.execute("DELETE FROM orders WHERE id=%s", (oid,))
            if cid:
                c.execute("DELETE FROM customers WHERE id=%s", (cid,))
            c.execute("DELETE FROM messages WHERE phone=%s OR to_number=%s", (PHONE, PHONE))
        s3 = boto3.client("s3", REGION)
        for o in s3.list_objects_v2(Bucket="brooklyn-bikery-sms-staging", Prefix="sms/").get("Contents", []):
            s3.delete_object(Bucket="brooklyn-bikery-sms-staging", Key=o["Key"])
        conn.close()

def test_send_invoice_flag():
    """The 'Text the customer their invoice' checkbox. sendInvoice=False saves the
    order but records NO outbound invoice message (and queues no SMS); sendInvoice=True
    records the outbound invoice. Default is True (older clients unchanged).
    Uses the messages table as the signal — the S3 job is drained by the trigger."""
    conn = _db()
    PHONE = "+19995558888"
    cid = oid = None
    def outbound_count():
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) n FROM messages WHERE phone=%s AND direction='outbound'", (PHONE,))
            return c.fetchone()["n"]
    try:
        today = time.strftime("%Y-%m-%d")
        with conn.cursor() as c:
            c.execute("DELETE FROM messages WHERE phone=%s OR to_number=%s", (PHONE, PHONE))
            c.execute("DELETE FROM customers WHERE phone=%s", (PHONE,))
            c.execute("INSERT INTO customers (tenant_id,name,phone,date_created) VALUES (1,'ZZ Invoice Test',%s,%s)", (PHONE, today)); cid = c.lastrowid
            c.execute("INSERT INTO orders (tenant_id,customer_id,date_of_service,backend_notes) VALUES (1,%s,%s,'inv')", (cid, today)); oid = c.lastrowid

        # 1) sendInvoice=False -> order edits, but NO outbound invoice recorded
        st, _ = _invoke_backend({"isNewCustomer": False, "orderId": oid, "lookupPhone": PHONE,
                                 "services": ["Replace Chain ($15)"], "notes": "noSMS", "sendInvoice": False})
        assert st == 200, f"edit (sendInvoice=false) expected 200, got {st}"
        assert outbound_count() == 0, "sendInvoice=false must record NO outbound invoice"
        with conn.cursor() as c:
            c.execute("SELECT price FROM orders WHERE id=%s", (oid,))
            assert float(c.fetchone()["price"]) == 15.0, "order should still be saved/edited"

        # 2) sendInvoice=True -> outbound invoice recorded
        st, _ = _invoke_backend({"isNewCustomer": False, "orderId": oid, "lookupPhone": PHONE,
                                 "services": ["Replace Chain ($15)"], "notes": "withSMS", "sendInvoice": True})
        assert st == 200, f"edit (sendInvoice=true) expected 200, got {st}"
        assert outbound_count() >= 1, "sendInvoice=true must record the outbound invoice"
        return "sendInvoice=False -> order saved, no invoice text; sendInvoice=True -> invoice queued"
    finally:
        with conn.cursor() as c:
            if oid:
                c.execute("DELETE FROM order_services WHERE order_id=%s", (oid,))
                c.execute("DELETE FROM orders WHERE id=%s", (oid,))
            if cid:
                c.execute("DELETE FROM customers WHERE id=%s", (cid,))
            c.execute("DELETE FROM messages WHERE phone=%s OR to_number=%s", (PHONE, PHONE))
        s3 = boto3.client("s3", REGION)
        for o in s3.list_objects_v2(Bucket="brooklyn-bikery-sms-staging", Prefix="sms/").get("Contents", []):
            s3.delete_object(Bucket="brooklyn-bikery-sms-staging", Key=o["Key"])
        conn.close()

def test_sms_compliance_and_status():
    """Opt-out suppresses invoice texts; consent is recorded on new customers;
    Twilio status callbacks (signed) update the message row; forged signatures
    are rejected. Guards the TCPA + delivery-status pipeline."""
    PHONE = "+15005550099"
    conn = _db()
    try:
        with conn.cursor() as c:
            # ── consent recording on a new customer ──────────────────────
            status, out = _invoke_backend({
                "isNewCustomer": True, "name": "Compliance Test", "phone": PHONE,
                "smsConsent": False, "serviceCodes": ["front_flat"],
                "sendInvoice": False, "notes": "", "bikeDescription": ""})
            assert status == 200, f"new-customer submit failed: {out}"
            c.execute("SELECT id, sms_consent FROM customers WHERE tenant_id=1 AND phone=%s", (PHONE,))
            row = c.fetchone()
            assert row and row["sms_consent"] == 0, f"consent not recorded: {row}"
            cid = row["id"]

            # ── opt-out suppression ──────────────────────────────────────
            c.execute("UPDATE customers SET sms_opted_out=1 WHERE id=%s", (cid,))
            status, out = _invoke_backend({
                "lookupPhone": PHONE, "isNewCustomer": False,
                "serviceCodes": ["front_flat"], "sendInvoice": True,
                "notes": "", "bikeDescription": ""})
            body = json.loads(out.get("body") or "{}")
            assert body.get("sms") == "optout", f"expected sms=optout, got {body}"

            # ── status callback: signed 'delivered' updates the row ──────
            c.execute("INSERT INTO messages (tenant_id, phone, direction, body, status, from_number, to_number) "
                      "VALUES (1, %s, 'outbound', 'status test', 'queued', '+15005550006', %s)", (PHONE, PHONE))
            # Use the insert cursor's lastrowid — NOT a separate
            # SELECT LAST_INSERT_ID(), which is connection-scoped and would
            # break when each statement runs on its own bridge connection.
            row_id = c.lastrowid
            c.execute("SELECT twilio_auth_token_secret_arn FROM tenants WHERE id=1")
            tok = _secret(c.fetchone()["twilio_auth_token_secret_arn"])
            auth_token = tok.get("auth_token") or tok.get("authToken")
            url = f"{STAGING['admin_api']}/AdminDashboard?msgRowId={row_id}"
            form = {"MessageSid": "SMstagingtest000000000000000000cafe",
                    "MessageStatus": "delivered", "From": "+15005550006", "To": PHONE}
            signing = url + "".join(f"{k}{v}" for k, v in sorted(form.items()))
            sig = base64.b64encode(hmac.new(auth_token.encode(), signing.encode(), hashlib.sha1).digest()).decode()
            assert _invoke_admin_webhook(form, sig, row_id) == 200
            c.execute("SELECT status FROM messages WHERE id=%s", (row_id,))
            assert c.fetchone()["status"] == "delivered", "status callback did not update row"

            # ── forged signature rejected ────────────────────────────────
            forged_status = _invoke_admin_webhook(form, "forged==", row_id)
            assert forged_status == 403, f"forged signature not rejected: {forged_status}"
        return "optout suppressed; consent stored; signed callback updates status; forged sig 403"
    finally:
        with conn.cursor() as c:
            c.execute("SELECT id FROM customers WHERE tenant_id=1 AND phone=%s", (PHONE,))
            r = c.fetchone()
            if r:
                cid = r["id"]
                c.execute("DELETE FROM order_services WHERE tenant_id=1 AND order_id IN (SELECT id FROM orders WHERE customer_id=%s)", (cid,))
                c.execute("DELETE FROM orders WHERE tenant_id=1 AND customer_id=%s", (cid,))
                c.execute("DELETE FROM customers WHERE id=%s", (cid,))
            c.execute("DELETE FROM messages WHERE phone=%s OR to_number=%s", (PHONE, PHONE))
        s3 = boto3.client("s3", REGION)
        for o in s3.list_objects_v2(Bucket="brooklyn-bikery-sms-staging", Prefix="sms/").get("Contents", []):
            s3.delete_object(Bucket="brooklyn-bikery-sms-staging", Key=o["Key"])
        conn.close()


def test_public_intake_tenant_routing():
    """The PUBLIC intake form must file each submission under the tenant that
    owns the request Origin — not always tenant 1. Guards the multi-tenant
    correctness of the customer-facing form (a second shop's customers must
    not land under Brooklyn Bikery). Also checks SMS consent is recorded."""
    import urllib.parse as _up
    conn = _db()
    T2_ORIGIN = "https://itest-tenant2.example.com"
    BB_ORIGIN = "https://staging.brooklynbikery.com"
    # Unique phones per run: the public form rate-limits 3 submissions/phone/hour
    # in-memory, so reusing a fixed phone across reruns within an hour would trip
    # a 429. Time-derived 10-digit numbers avoid that and are still scrubbed.
    _base = int(time.time())
    P2, P1 = str(_base), str(_base + 1)

    def intake(origin, name, phone):
        form = _up.urlencode({"name": name, "phone": phone, "notes": "itest routing",
                              "serviceConsent": "on", "marketingConsent": "on"})
        event = {"httpMethod": "POST", "requestContext": {"http": {"method": "POST"}},
                 "headers": {"origin": origin, "content-type": "application/x-www-form-urlencoded"},
                 "body": form, "isBase64Encoded": False}
        r = lam.invoke(FunctionName=STAGING["customer_fn"], Payload=json.dumps(event).encode())
        out = json.loads(r["Payload"].read())
        return out.get("statusCode")

    def filed_under(phone):
        with conn.cursor() as c:
            c.execute("SELECT id, tenant_id, sms_consent FROM customers WHERE phone=%s", (phone,))
            cu = c.fetchone()
            if not cu: return None
            c.execute("SELECT tenant_id FROM orders WHERE customer_id=%s ORDER BY id DESC LIMIT 1", (cu["id"],))
            o = c.fetchone()
            return {"cust_tenant": cu["tenant_id"], "order_tenant": o["tenant_id"] if o else None,
                    "sms_consent": cu["sms_consent"]}

    def scrub():
        with conn.cursor() as c:
            for p in (P1, P2):
                c.execute("DELETE o FROM orders o JOIN customers cu ON cu.id=o.customer_id WHERE cu.phone=%s", (p,))
                c.execute("DELETE FROM customers WHERE phone=%s", (p,))

    with conn.cursor() as c:
        c.execute("SELECT allowed_origin FROM tenants WHERE id=2")
        old_origin = c.fetchone()["allowed_origin"]
    try:
        scrub()
        with conn.cursor() as c:
            c.execute("UPDATE tenants SET allowed_origin=%s WHERE id=2", (T2_ORIGIN,))
        # staging TENANT_ORIGIN_TTL is 2s; wait past it so the Lambda reloads.
        time.sleep(4)

        assert intake(T2_ORIGIN, "Route T2", P2) == 200, "tenant-2 intake failed"
        assert intake(BB_ORIGIN, "Route BB", P1) == 200, "tenant-1 intake failed"

        t2 = filed_under(P2); bb = filed_under(P1)
        assert t2 == {"cust_tenant": 2, "order_tenant": 2, "sms_consent": 1}, f"tenant-2 misrouted: {t2}"
        assert bb == {"cust_tenant": 1, "order_tenant": 1, "sms_consent": 1}, f"tenant-1 misrouted: {bb}"
        return "intake routes by Origin: shop-2 origin -> tenant 2, BB origin -> tenant 1; consent recorded"
    finally:
        scrub()
        with conn.cursor() as c:
            c.execute("UPDATE tenants SET allowed_origin=%s WHERE id=2", (old_origin,))
        conn.close()


def test_change_password():
    """Authenticated self-service password change: wrong current -> 401, too
    short -> 400, valid change rotates the secret so the old password stops
    working and the new one logs in. Restores the original password via a
    direct secret write (the original may be shorter than the 8-char API
    minimum, so it can't be restored through the endpoint)."""
    api = STAGING["admin_api"] + "/AdminDashboard"
    orig = _secret(STAGING["admin_pw_secret"])["password"]
    TEMP = "itest-temp-pw-9animals"
    try:
        _, b = _http(api, {"action": "login", "password": orig})
        tok = b["token"]
        # wrong current password -> 403 (NOT 401: session is valid, so a 401
        # would make the frontend log the admin out instead of showing the error)
        sc, _ = _http(api, {"action": "change-password", "currentPassword": "definitely-wrong",
                            "newPassword": "another-good-1"}, tok)
        assert sc == 403, f"wrong current should be 403, got {sc}"
        # too short
        sc, _ = _http(api, {"action": "change-password", "currentPassword": orig,
                            "newPassword": "short"}, tok)
        assert sc == 400, f"short new password should be 400, got {sc}"
        # valid change
        sc, _ = _http(api, {"action": "change-password", "currentPassword": orig,
                            "newPassword": TEMP}, tok)
        assert sc == 200, f"valid change should be 200, got {sc}"
        # old password no longer works; new one does
        sc_old, _ = _http(api, {"action": "login", "password": orig})
        sc_new, _ = _http(api, {"action": "login", "password": TEMP})
        assert sc_old == 401, f"old password should fail after change, got {sc_old}"
        assert sc_new == 200, f"new password should work after change, got {sc_new}"
        return "wrong-current 403; short 400; valid rotates secret (old fails, new works)"
    finally:
        # Restore the original password directly (bypasses the 8-char API rule).
        sm.put_secret_value(SecretId=STAGING["admin_pw_secret"],
                            SecretString=json.dumps({"password": orig}))


def test_spoke_pricing_data_driven():
    """Spoke (quantity-based) pricing is per-shop DATA now, not a hardcoded
    33+2*qty in the Lambda: editing the catalog row's first-spoke + each-
    additional amounts changes what a spoke order charges. Restores originals."""
    conn = _db()
    api = STAGING["admin_api"] + "/AdminDashboard"
    pw = _secret(STAGING["admin_pw_secret"])["password"]
    _, b = _http(api, {"action": "login", "password": pw}); tok = b["token"]
    with conn.cursor() as c:
        c.execute("SELECT id, display_name, default_price, additional_unit_price, category, "
                  "sort_order, is_active FROM service_catalog WHERE tenant_id=1 AND code='front_fix_spoke'")
        o = c.fetchone()
    sid = o["id"]
    def save(price, add):
        return _http(api, {"action": "catalog-save", "service": {
            "id": sid, "name": o["display_name"], "price": price, "add_price": add,
            "category": o["category"], "active": bool(o["is_active"]), "sort": o["sort_order"]}}, tok)[0]
    PHONE = "+15005557001"
    try:
        assert save(50, 7) == 200, "catalog-save spoke failed"
        with conn.cursor() as c:
            c.execute("DELETE o FROM orders o JOIN customers cu ON cu.id=o.customer_id WHERE cu.phone=%s", (PHONE,))
            c.execute("DELETE FROM customers WHERE tenant_id=1 AND phone=%s", (PHONE,))
            c.execute("INSERT INTO customers (tenant_id,name,phone,date_created) VALUES (1,'Spoke ITest',%s,CURDATE())", (PHONE,))
            cid = c.lastrowid
            c.execute("INSERT INTO orders (tenant_id,customer_id,date_of_service) VALUES (1,%s,CURDATE())", (cid,))
            oid = c.lastrowid
        st, out = _invoke_backend({"isNewCustomer": False, "orderId": oid, "lookupPhone": PHONE,
                                   "serviceCodes": [], "frontSpokes": 2, "sendInvoice": False,
                                   "notes": "", "bikeDescription": ""})
        assert st == 200, f"spoke order submit failed: {st}"
        with conn.cursor() as c:
            c.execute("SELECT price FROM orders WHERE id=%s", (oid,))
            price = float(c.fetchone()["price"])
        # first $50 + each-additional $7 * (2-1) = $57 (NOT the old 33+2*2=37)
        assert abs(price - 57.0) < 0.01, f"2 spokes @ 50/7 should be $57, got ${price}"
        return "spoke pricing is data-driven: first $50 + $7 each -> 2 spokes = $57"
    finally:
        save(float(o["default_price"]), float(o["additional_unit_price"]))  # restore
        with conn.cursor() as c:
            c.execute("SELECT id FROM customers WHERE tenant_id=1 AND phone=%s", (PHONE,))
            r = c.fetchone()
            if r:
                cid = r["id"]
                c.execute("DELETE FROM order_services WHERE order_id IN (SELECT id FROM orders WHERE customer_id=%s AND tenant_id=1)", (cid,))
                c.execute("DELETE FROM orders WHERE customer_id=%s AND tenant_id=1", (cid,))
                c.execute("DELETE FROM customers WHERE id=%s", (cid,))
        conn.close()


TESTS = [test_login_and_auth, test_data_isolation, test_wrong_order_regression,
         test_sms_cannot_deliver, test_tenant_isolation, test_login_returns_shop,
         test_new_customer_flow, test_send_invoice_flag, test_sms_compliance_and_status,
         test_public_intake_tenant_routing, test_change_password, test_spoke_pricing_data_driven]

def main():
    print("Running staging integration tests against the -staging stack...\n")
    import traceback
    failed = 0
    for t in TESTS:
        try:
            detail = t()
            print(f"  [PASS] {t.__name__}: {detail}")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()  # full trace in CI output for diagnosis
    print(f"\n{len(TESTS)-failed}/{len(TESTS)} passed.")
    if failed:
        print("STAGING TESTS FAILED — do not promote to prod.")
        sys.exit(1)
    print("All staging tests passed.")

if __name__ == "__main__":
    main()
