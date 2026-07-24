"""
Admin Authentication and Dashboard Lambda
Handles admin login with JWT generation, order search functionality, and SMS queueing.
Implements rate limiting for login attempts and requires JWT auth for all protected endpoints.
"""

import json
import os
import uuid
import traceback
import hmac
import hashlib
import base64
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pymysql

# ============================================
# CONFIGURATION
# ============================================
REGION = os.getenv("AWS_REGION", "us-east-1")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://brooklynbikery.com")
TOKEN_EXPIRY_SECONDS = 8 * 60 * 60  # 8 hours - JWT token validity period

# Rate limiting for login attempts (in-memory, resets on Lambda cold start)
login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 minute lockout after max attempts

# ── Multi-origin CORS ────────────────────────────────────────────────────────
# Each tenant can serve its admin pages from its own origin (future
# {shop}.bluewrenchhq.com). The allow-list is: the env-var origin (prod or
# staging default) + every active tenant's `allowed_origin`. The request's
# Origin header is reflected back ONLY if it's in that list; anything else
# gets the env default (which the browser then correctly blocks).
_ORIGIN_CACHE = {"origins": None, "loaded_at": 0.0}
_ORIGIN_TTL_SECONDS = 300
_REQUEST_ORIGIN = ALLOWED_ORIGIN  # per-invocation; one request per container


def _allowed_origins():
    """Set of origins allowed to call this API (env default + per-tenant)."""
    now = time.time()
    if _ORIGIN_CACHE["origins"] is not None and now - _ORIGIN_CACHE["loaded_at"] < _ORIGIN_TTL_SECONDS:
        return _ORIGIN_CACHE["origins"]
    origins = {ALLOWED_ORIGIN}
    try:
        secret = get_secret()
        conn = pymysql.connect(host=secret["host"], user=secret["user"],
                               password=secret["password"], database=secret["database"],
                               connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT allowed_origin FROM tenants WHERE status='active' AND allowed_origin IS NOT NULL AND allowed_origin != ''")
                for (o,) in cur.fetchall():
                    origins.add(o.strip().rstrip("/"))
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️ allowed-origins load failed, using cached/default: {e}")
        if _ORIGIN_CACHE["origins"]:
            return _ORIGIN_CACHE["origins"]
    _ORIGIN_CACHE["origins"] = origins
    _ORIGIN_CACHE["loaded_at"] = now
    return origins


def resolve_request_origin(event):
    """Pick the CORS origin to reflect for this request."""
    global _REQUEST_ORIGIN
    headers = event.get("headers") or {}
    origin = (headers.get("origin") or headers.get("Origin") or "").strip().rstrip("/")
    _REQUEST_ORIGIN = origin if origin and origin in _allowed_origins() else ALLOWED_ORIGIN


def cors_headers():
    return {
        "Access-Control-Allow-Origin": _REQUEST_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Vary": "Origin",
    }


def response(status, body):
    """Helper to format API Gateway response with CORS headers"""
    return {"statusCode": status, "headers": cors_headers(), "body": json.dumps(body)}

# ============================================
# SECRETS MANAGEMENT
# ============================================
def get_secret():
    """Retrieve database credentials from AWS Secrets Manager"""
    client = boto3.client("secretsmanager", region_name=REGION)
    resp = client.get_secret_value(SecretId=os.getenv("DB_SECRET_ID", "bikeshop-credentials"))
    return json.loads(resp["SecretString"])

# ============================================
# TENANT CONFIG (multi-tenancy migration, step 7)
# Per-warm-container cache. tenant_id hardcoded to 1 (Brooklyn Bikery) at
# call sites until step 8 wires up URL-based tenant resolution.
# ============================================
_TENANT_CACHE: dict = {}

def get_tenant(tenant_id: int = 1) -> dict:
    """
    Load per-tenant config from the `tenants` table. Cached for the lifetime
    of the warm container.
    """
    if tenant_id in _TENANT_CACHE:
        return _TENANT_CACHE[tenant_id]
    secret = get_secret()  # bikeshop-credentials (shared DB)
    conn = pymysql.connect(
        host=secret["host"],
        user=secret["user"],
        password=secret["password"],
        database=secret["database"],
        connect_timeout=5,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, display_name, phone, address, tax_rate, "
                "allowed_origin, twilio_account_sid, twilio_auth_token_secret_arn, "
                "twilio_from_number, sms_sender_name, admin_password_secret_arn, "
                "status FROM tenants WHERE id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Tenant {tenant_id} not found")
            cols = [d[0] for d in cur.description]
            t = dict(zip(cols, row))
            if t["status"] != "active":
                raise RuntimeError(
                    f"Tenant {tenant_id} ({t['slug']}) status is {t['status']!r}"
                )
            _TENANT_CACHE[tenant_id] = t
            return t
    finally:
        conn.close()

def resolve_tenant_id(payload=None, body=None):
    """Tenant for this request (multi-tenancy step 8).

    Prefer the verified JWT claim (set at login); else a login slug in the body;
    else default to Brooklyn Bikery (tenant 1). The default keeps existing
    single-tenant traffic and any pre-step-8 tokens behaving EXACTLY as before.
    """
    if payload and payload.get("tenant_id"):
        try:
            return int(payload["tenant_id"])
        except (TypeError, ValueError):
            return 1
    slug = (body or {}).get("tenant") or (body or {}).get("shop")
    if slug:
        secret = get_secret()
        conn = pymysql.connect(host=secret["host"], user=secret["user"],
                               password=secret["password"], database=secret["database"],
                               connect_timeout=5, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM tenants WHERE slug = %s AND status = 'active'", (slug,))
                row = cur.fetchone()
                if row:
                    return int(row[0])
        finally:
            conn.close()
    return 1

def get_admin_password(tenant_id: int = 1):
    """
    Retrieve admin password from AWS Secrets Manager. The secret ARN is now
    per-tenant (was hardcoded to 'bikery-admin-password' before step 7).
    Used for verifying login attempts.
    """
    try:
        tenant = get_tenant(tenant_id)
        client = boto3.client("secretsmanager", region_name=REGION)
        resp = client.get_secret_value(SecretId=tenant["admin_password_secret_arn"])
        secret_data = json.loads(resp["SecretString"])
        return secret_data.get("password", "")
    except Exception as e:
        print(f"Error fetching admin password: {e}")
        return None

def get_jwt_secret():
    """
    Get JWT signing secret from Secrets Manager.

    The secret MUST be pre-provisioned in IaC. This function used to
    auto-create the secret on ResourceNotFoundException, but that's a
    rotation race condition (concurrent cold starts each create-then-
    overwrite, instantly invalidating live tokens).
    """
    client = boto3.client("secretsmanager", region_name=REGION)
    secret_id = os.getenv("JWT_SECRET_ID", "bikery-jwt-secret")
    try:
        resp = client.get_secret_value(SecretId=secret_id)
        secret_data = json.loads(resp["SecretString"])
        secret = secret_data.get("secret", "")
        if not secret:
            print("CRITICAL: bikery-jwt-secret exists but has empty 'secret' field")
            raise RuntimeError("JWT secret misconfigured")
        return secret
    except client.exceptions.ResourceNotFoundException:
        print("CRITICAL: bikery-jwt-secret not provisioned. Create it in Secrets Manager.")
        raise
    except Exception as e:
        print(f"Error fetching JWT secret: {e}")
        raise

def get_twilio_auth_token():
    """Fetch Twilio auth token from Secrets Manager for webhook signature validation.
    Secret ARN is now per-tenant (step 7). tenant_id=1 hardcoded until step 8."""
    try:
        tenant = get_tenant(1)
        client = boto3.client("secretsmanager", region_name=REGION)
        resp = client.get_secret_value(SecretId=tenant["twilio_auth_token_secret_arn"])
        secret_data = json.loads(resp["SecretString"])
        # Try both common key names
        return secret_data.get("auth_token") or secret_data.get("authToken") or ""
    except Exception as e:
        print(f"Error fetching Twilio auth token: {e}")
        return None

# ============================================
# MULTI-TENANCY: ORDER SERVICES SYNTHESIS (step 5)
# ============================================
#
# The legacy data model stored each performed service as a boolean column on
# `orders` (front_flat, tune_up, ...) plus integer spoke counts and a custom
# service trio (custom_service / custom_description / custom_service_price).
#
# Step 4 made Backend-Form.py dual-write to the new `order_services` line-item
# table while still writing the legacy boolean columns. Step 5 (this code)
# stops READING the legacy columns and instead reconstructs the same shape
# from `order_services` in Python, so the API response stays identical for
# the frontend. Step 6 will drop the legacy boolean columns from `orders`.
#
# The list below is the canonical set of "fixed" boolean service columns the
# frontend expects. Keep in sync with Backend-Form.py:340-381 and migration
# 003's service_catalog seed.

_LEGACY_FIXED_SERVICE_COLUMNS = [
    # Flat tire repairs
    "front_flat", "rear_flat", "front_flat_ebike", "rear_flat_ebike",
    # Brake adjustments
    "front_brake_adj", "rear_brake_adj",
    "front_brake_adj_ebike", "rear_brake_adj_ebike",
    # Brake pads
    "front_replace_vbrake_pads", "rear_replace_vbrake_pads",
    "front_new_vbrake_pads", "rear_new_vbrake_pads",
    "front_replace_disc_pads", "rear_replace_disc_pads",
    "front_new_disc_pads", "rear_new_disc_pads",
    "front_hydraulic_brake_bleed", "rear_hydraulic_brake_bleed",
    # Tune-up
    "tune_up",
    # Derailleur
    "front_derailleur_adj", "rear_derailleur_adj",
    # Drivetrain
    "replace_cassette", "new_bb", "replace_chain", "replace_crank_bb",
    # Cables and lines
    "replace_front_brake_line", "replace_rear_brake_line",
    "replace_front_gear_line", "replace_rear_gear_line",
    # Wheels
    "front_wheel_true", "rear_wheel_true",
    "front_repack_wheel", "rear_repack_wheel",
    "replace_front_rotor", "replace_rear_rotor",
    # Headset
    "repack_headset", "repack_headset_ebike",
    # E-bike and assembly
    "ebike_diagnostic", "bike_assembly", "ebike_assembly",
]


def synthesize_order_services(cursor, tenant_id, order_ids):
    """
    Reconstruct the legacy boolean-column shape for the given orders, sourced
    from `order_services` rows joined to `service_catalog`. Returns a dict
    `{order_id: {column_name: value, ...}}`. Every order_id in the input list
    gets an entry, defaulted to "no services performed" so callers can merge
    blindly without KeyErrors on empty orders.

    The output mirrors what the legacy SELECT used to produce:
      - Fixed services: 0 or 1
      - Spokes: integer count (0 = no spokes)
      - custom_service: 0 or 1
      - custom_description: text or None
      - custom_service_price: float or None
    """
    if not order_ids:
        return {}

    # Initialize every order with "no services" defaults so merging is safe.
    defaults = {col: 0 for col in _LEGACY_FIXED_SERVICE_COLUMNS}
    defaults.update({
        "front_fix_spoke": 0,
        "rear_fix_spoke": 0,
        "custom_service": 0,
        "custom_description": None,
        "custom_service_price": None,
    })
    result = {oid: dict(defaults) for oid in order_ids}

    placeholders = ", ".join(["%s"] * len(order_ids))
    cursor.execute(
        f"""
        SELECT os.order_id,
               sc.code,
               sc.pricing_formula,
               os.quantity,
               os.price_charged,
               os.notes
        FROM order_services os
        JOIN service_catalog sc ON sc.id = os.service_catalog_id
        WHERE os.tenant_id = %s AND os.order_id IN ({placeholders})
        """,
        (tenant_id, *order_ids),
    )

    for order_id, code, formula, quantity, price_charged, notes in cursor.fetchall():
        slot = result.get(order_id)
        if slot is None:
            continue  # row references an order we didn't ask for; skip
        if formula == "spoke":
            # front_fix_spoke / rear_fix_spoke is an integer count, not a bool.
            slot[code] = int(quantity)
        elif formula == "custom":
            slot["custom_service"] = 1
            slot["custom_description"] = notes
            slot["custom_service_price"] = (
                float(price_charged) if price_charged is not None else None
            )
        else:
            # Fixed-price service: set the corresponding boolean to 1.
            slot[code] = 1

    return result


def validate_twilio_signature(event, raw_body):
    """
    Validate Twilio's X-Twilio-Signature against the request.
    Returns True if valid, False if missing/invalid/can't be checked.
    Reference: https://www.twilio.com/docs/usage/webhooks/webhooks-security
    """
    headers = event.get("headers") or {}
    # Find the header case-insensitively (API Gateway v2 lowercases)
    sig_header_key = next((k for k in headers if k.lower() == "x-twilio-signature"), None)
    if not sig_header_key:
        print("WARN: missing X-Twilio-Signature header")
        return False
    twilio_sig = headers[sig_header_key]

    auth_token = get_twilio_auth_token()
    if not auth_token:
        print("WARN: Twilio auth token unavailable, refusing to validate")
        return False

    # Reconstruct the URL Twilio called.
    # Prefer rawPath (preserves stage segment on default execute-api URLs);
    # fall back to http.path / path for HTTP API custom-domain mappings.
    rc = event.get("requestContext", {}) or {}
    domain = rc.get("domainName") or headers.get("Host") or headers.get("host") or ""
    http_ctx = rc.get("http") or {}
    path = (event.get("rawPath")
            or http_ctx.get("path")
            or rc.get("path")
            or event.get("path")
            or "")
    proto = (headers.get("X-Forwarded-Proto")
             or headers.get("x-forwarded-proto")
             or "https")
    url = f"{proto}://{domain}{path}"
    raw_qs = event.get("rawQueryString", "")
    if raw_qs:
        url += f"?{raw_qs}"
    # Log once at INFO so misconfig (URL mismatch with what's in Twilio console)
    # is detectable from CloudWatch without enabling debug logging.
    print(f"Twilio sig validation: reconstructed url={url}")

    # Twilio appends POST params sorted alphabetically by key, value concatenated
    from urllib.parse import parse_qs
    form = parse_qs(raw_body, keep_blank_values=True)
    signing_string = url + "".join(
        f"{k}{(v[0] if isinstance(v, list) else v)}" for k, v in sorted(form.items())
    )
    expected = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, twilio_sig)

# ============================================
# JWT IMPLEMENTATION
# ============================================
def base64url_encode(data):
    """
    Base64 URL-safe encoding without padding (JWT standard).
    Removes padding characters (=) as they're not URL-safe.
    """
    if isinstance(data, str):
        data = data.encode('utf-8')
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def base64url_decode(data):
    """
    Base64 URL-safe decoding with automatic padding restoration.
    JWT tokens don't include padding, so we add it back.
    """
    padding = 4 - len(data) % 4
    if padding != 4:
        data += '=' * padding
    return base64.urlsafe_b64decode(data)

def create_jwt(payload, secret):
    """
    Create a JWT token with HMAC-SHA256 signature.
    Token format: header.payload.signature
    
    Args:
        payload: Dictionary containing claims (e.g., role, ip)
        secret: Signing secret from Secrets Manager
    
    Returns:
        JWT token string
    """
    header = {"alg": "HS256", "typ": "JWT"}
    
    # Add standard claims
    now = int(time.time())
    payload["iat"] = now  # Issued at
    payload["exp"] = now + TOKEN_EXPIRY_SECONDS  # Expiration
    
    # Encode header and payload
    header_b64 = base64url_encode(json.dumps(header))
    payload_b64 = base64url_encode(json.dumps(payload))
    
    # Create signature using HMAC-SHA256
    message = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    signature_b64 = base64url_encode(signature)
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"

def verify_jwt(token):
    """
    Verify JWT token signature and expiration.
    Returns: (payload_dict, None) on success or (None, error_message) on failure
    """
    try:
        secret = get_jwt_secret()
        if not secret:
            return None, "Auth service unavailable"
        
        # Split token into components
        parts = token.split('.')
        if len(parts) != 3:
            return None, "Invalid token format"
        
        header_b64, payload_b64, signature_b64 = parts

        # Validate the alg header — refuse anything other than HS256.
        # Defense against future alg-confusion attacks if RS/ES support is ever
        # added (or against "none"/"None" alg attacks).
        try:
            header = json.loads(base64url_decode(header_b64))
        except Exception:
            return None, "Invalid token header"
        if header.get("alg") != "HS256":
            return None, "Invalid token algorithm"

        # Verify signature
        message = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(
            secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        actual_sig = base64url_decode(signature_b64)
        
        # Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None, "Invalid signature"
        
        # Decode and check expiration
        payload = json.loads(base64url_decode(payload_b64))
        
        if payload.get("exp", 0) < int(time.time()):
            return None, "Token expired"
        
        return payload, None
        
    except Exception as e:
        print(f"JWT verification error: {e}")
        return None, "Token verification failed"

def require_auth(event):
    """
    Extract and verify JWT from Authorization header.
    Returns: (payload, None) on success or (None, error_message) on failure
    """
    headers = event.get('headers', {}) or {}
    # Check both lowercase and capitalized (API Gateway inconsistency)
    auth_header = headers.get('authorization', '') or headers.get('Authorization', '')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, "Authorization required"
    
    # Extract token after "Bearer " prefix
    token = auth_header[7:]
    return verify_jwt(token)

# ============================================
# RATE LIMITING
# ============================================
def check_login_rate_limit(ip_address):
    """
    Check if IP address is rate limited for login attempts.
    Enforces 5 attempts per IP with 5-minute lockout.
    
    Returns: (allowed: bool, error_message: str or None)
    """
    now = time.time()
    
    if ip_address in login_attempts:
        attempts, first_attempt, locked_until = login_attempts[ip_address]
        
        # Check if still locked out
        if locked_until and now < locked_until:
            remaining = int(locked_until - now)
            return False, f"Too many login attempts. Try again in {remaining} seconds."
        
        # Reset if lockout expired
        if locked_until and now >= locked_until:
            login_attempts[ip_address] = (0, now, None)
        # Reset if 15 minutes passed since first attempt
        elif now - first_attempt > 900:
            login_attempts[ip_address] = (0, now, None)
    
    return True, None

def record_login_attempt(ip_address, success):
    """
    Record a login attempt and apply lockout if needed.
    Successful login clears all attempts for that IP.
    """
    now = time.time()
    
    if success:
        # Clear attempts on successful login
        if ip_address in login_attempts:
            del login_attempts[ip_address]
        return
    
    # Increment failed attempts
    if ip_address in login_attempts:
        attempts, first_attempt, locked_until = login_attempts[ip_address]
        attempts += 1
        
        # Lock out if max attempts reached
        if attempts >= MAX_LOGIN_ATTEMPTS:
            login_attempts[ip_address] = (attempts, first_attempt, now + LOCKOUT_SECONDS)
        else:
            login_attempts[ip_address] = (attempts, first_attempt, None)
    else:
        # First failed attempt
        login_attempts[ip_address] = (1, now, None)

# ============================================
# PUSH NOTIFICATIONS (via S3 trigger → Send-SMS Lambda)
# ============================================
# Push jobs go to a SEPARATE private bucket. The legacy bucket
# `brooklyn-bikery-sms` is public-read for the `mms-images/` prefix; even
# though `push/` was prefix-scoped private, putting subscription endpoints +
# auth secrets in any bucket that's partly public is a fragility we don't want.
# `brooklyn-bikery-push-jobs` has BlockPublicAccess on at the bucket level.
PUSH_BUCKET = os.getenv("PUSH_BUCKET", "brooklyn-bikery-push-jobs")

def send_push_notifications(from_number, message_body, db_secret, tenant_id=1):
    """
    Send push notifications by writing a job file to S3 push/ folder.
    The Send-SMS Lambda (not in VPC) picks it up via S3 trigger and
    sends the actual webpush request to Apple/Google push servers.

    NOTE: VAPID private key is NOT included in the S3 job anymore.
    Send-SMS.py fetches it directly from Secrets Manager. This avoids
    leaking the private key if the bucket policy ever loosens.
    """
    print("🔔 Step 1: Querying push subscriptions...")

    # Get push subscriptions AND customer name in one DB connection
    conn = pymysql.connect(
        host=db_secret["host"],
        user=db_secret["user"],
        password=db_secret["password"],
        database=db_secret["database"],
        connect_timeout=5,
    )
    cursor = conn.cursor()
    cursor.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE tenant_id = %s", (tenant_id,))
    subscriptions = cursor.fetchall()

    # Look up customer name by phone number (try both E.164 and 10-digit)
    digits = ''.join(c for c in from_number if c.isdigit())
    phone_10 = digits[-10:] if len(digits) >= 10 else digits
    cursor.execute(
        "SELECT name FROM customers WHERE tenant_id = %s AND (phone = %s OR phone = %s OR phone = %s) LIMIT 1",
        (tenant_id, from_number, phone_10, '+1' + phone_10)
    )
    name_row = cursor.fetchone()
    customer_name = name_row[0] if name_row and name_row[0] else None

    cursor.close()
    conn.close()
    print(f"🔔 Step 3: Found {len(subscriptions)} subscription(s), customer={customer_name}")

    if not subscriptions:
        print("📭 No push subscriptions found")
        return

    # Truncate message for notification preview
    preview = message_body[:100] + "..." if len(message_body) > 100 else message_body

    # Use customer name if known, otherwise fall back to formatted phone number
    if customer_name:
        display_sender = customer_name
    else:
        display_sender = from_number
        if from_number.startswith("+1") and len(from_number) == 12:
            display_sender = f"({from_number[2:5]}) {from_number[5:8]}-{from_number[8:]}"

    payload = json.dumps({
        "title": display_sender,
        "body": preview,
        "icon": "https://brooklynbikery.com/favicon.png",
        "badge": "https://brooklynbikery.com/favicon.png",
        "data": {"phone": from_number, "name": customer_name or display_sender}
    })

    # Write push job to S3 — triggers Send-SMS Lambda via S3 event.
    # VAPID private key intentionally OMITTED here — Send-SMS fetches it from
    # Secrets Manager so it never sits in S3 (which is public-read for mms-images/).
    push_job = json.dumps({
        "action": "send_push",
        "subscriptions": [
            {"endpoint": ep, "p256dh": p256, "auth": au}
            for ep, p256, au in subscriptions
        ],
        "payload": payload,
        "vapid_claims": {"sub": "mailto:admin@brooklynbikery.com"}
    })

    s3_client = boto3.client("s3")
    job_key = f"push/{uuid.uuid4().hex}.json"
    print(f"🔔 Step 4: Writing push job to s3://{PUSH_BUCKET}/{job_key}")
    s3_client.put_object(
        Bucket=PUSH_BUCKET,
        Key=job_key,
        Body=push_job.encode("utf-8"),
        ContentType="application/json"
    )
    print(f"🔔 Push job written successfully")


# ============================================
# MAIN HANDLER
# ============================================
def lambda_handler(event, context):
    """
    Main handler for admin authentication and dashboard operations.
    
    Endpoints:
    - POST /login (action=login): Authenticate admin and return JWT
    - POST /send-sms: Queue SMS message (requires auth)
    - POST /search (default): Search orders by date/customer (requires auth)
    """
    print("🔍 Event received:", json.dumps(event))

    # Extract HTTP method (API Gateway format varies)
    http_method = (
        event.get('requestContext', {}).get('http', {}).get('method') or  # HTTP API v2
        event.get('requestContext', {}).get('httpMethod') or  # REST API
        event.get('httpMethod')  # Also REST API
    )
    
    # Resolve which origin to reflect in CORS headers for THIS request
    # (multi-origin: per-tenant frontends). Must run before any response.
    resolve_request_origin(event)

    # Handle CORS preflight request
    if http_method == 'OPTIONS':
        return {
            "statusCode": 200,
            "headers": {**cors_headers(), "Access-Control-Max-Age": "86400"},
            "body": ""
        }
    
    # Get client IP for rate limiting
    ip_address = (
        event.get('requestContext', {}).get('http', {}).get('sourceIp') or
        event.get('requestContext', {}).get('identity', {}).get('sourceIp') or
        'unknown'
    )
    
    # Extract route information (different API Gateway versions use different fields)
    raw_path = event.get('rawPath', '')
    route_key = event.get('routeKey', '')
    path = event.get('path', '')
    resource = event.get('resource', '')
    
    # Parse request body
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event.get("body", "{}"))
        except:
            body = {}
    
    # ============================================
    # LOGIN ENDPOINT (No auth required)
    # ============================================
    if body.get("action") == "login" and http_method == 'POST':
        print("🔐 Processing login request")
        
        # Check rate limit first
        allowed, error_msg = check_login_rate_limit(ip_address)
        if not allowed:
            return response(429, {"error": error_msg})
        
        try:
            provided_password = body.get("password", "")
            
            if not provided_password:
                record_login_attempt(ip_address, False)
                return response(400, {"error": "Password required"})
            
            # Multi-tenancy step 8: which shop is logging in? A slug in the body
            # (absent -> Brooklyn Bikery / tenant 1). Validate against THAT
            # tenant's password and stamp the tenant into the JWT below.
            login_tid = resolve_tenant_id(body=body)
            correct_password = get_admin_password(login_tid)
            if not correct_password:
                return response(500, {"error": "Authentication service unavailable"})
            
            # Timing-safe password comparison (prevents timing attacks)
            password_match = hmac.compare_digest(
                provided_password.encode('utf-8'),
                correct_password.encode('utf-8')
            )
            
            if not password_match:
                record_login_attempt(ip_address, False)
                print(f"❌ Failed login attempt from {ip_address}")
                return response(401, {"error": "Invalid password"})
            
            # Success - generate JWT token
            record_login_attempt(ip_address, True)
            print(f"✅ Successful login from {ip_address}")
            
            jwt_secret = get_jwt_secret()
            token = create_jwt({"role": "admin", "ip": ip_address, "tenant_id": login_tid}, jwt_secret)

            # Shop name for the dashboard header so the user always knows which
            # shop they're in (multi-tenant). Cached get_tenant — no extra round-trip.
            try:
                shop_name = get_tenant(login_tid).get("display_name", "")
            except Exception:
                shop_name = ""

            return response(200, {
                "message": "Login successful",
                "token": token,
                "shop": shop_name,
                "expires_in": TOKEN_EXPIRY_SECONDS
            })
            
        except Exception as e:
            print(f"Login error: {e}")
            traceback.print_exc()
            return response(500, {"error": "Authentication failed"})

    # ============================================
    # PING ENDPOINT (No auth - for Lambda warmup)
    # ============================================
    if body.get("action") == "ping":
        print("🏓 Ping received — Lambda is warm")
        return response(200, {"ok": True, "ts": int(time.time())})

    # ============================================
    # BRANDING ENDPOINT (No auth — login screens)
    # The login page needs the shop's display name BEFORE anyone logs in
    # (a shop's staff should see their own shop's name, not Brooklyn
    # Bikery's). Public by design; exposes nothing but slug -> display name
    # for active tenants.
    # ============================================
    if body.get("action") == "branding":
        try:
            slug = (body.get("tenant") or "").strip().lower()
            secret = get_secret()
            conn = pymysql.connect(host=secret["host"], user=secret["user"],
                                   password=secret["password"], database=secret["database"],
                                   connect_timeout=5, charset="utf8mb4")
            try:
                with conn.cursor() as cur:
                    if slug:
                        cur.execute("SELECT display_name FROM tenants WHERE slug=%s AND status='active'", (slug,))
                    else:
                        cur.execute("SELECT display_name FROM tenants WHERE id=1 AND status='active'")
                    row = cur.fetchone()
            finally:
                conn.close()
            if not row:
                return response(404, {"error": "Unknown shop"})
            return response(200, {"displayName": row[0]})
        except Exception as e:
            print(f"❌ branding lookup failed: {e}")
            return response(500, {"error": "Branding unavailable"})

    # ============================================
    # TWILIO INBOUND WEBHOOK (No auth - public endpoint)
    # Receives incoming SMS from Twilio and stores in database
    # Detects Twilio by checking for MessageSid in form-encoded body
    # ============================================
    is_twilio_webhook = False

    # Check if this looks like a Twilio webhook (form-encoded with MessageSid)
    if http_method == 'POST':
        raw_body = event.get("body", "")
        if event.get("isBase64Encoded") and raw_body:
            try:
                raw_body = base64.b64decode(raw_body).decode('utf-8')
            except:
                raw_body = ""
        # Twilio sends MessageSid in form-encoded data
        if 'MessageSid' in raw_body and 'From' in raw_body:
            is_twilio_webhook = True

    if is_twilio_webhook:
        try:
            print("📨 Handling Twilio inbound webhook")

            # Twilio sends form-encoded data, need to parse it
            webhook_body = event.get("body", "")

            # Check if it's base64 encoded (API Gateway sometimes does this)
            if event.get("isBase64Encoded"):
                webhook_body = base64.b64decode(webhook_body).decode('utf-8')

            # CRITICAL: validate Twilio signature before any DB writes / push fan-out.
            # Without this, anyone can spoof "customer replies" and trigger push storms.
            if not validate_twilio_signature(event, webhook_body):
                print("⛔ Twilio signature validation failed — rejecting webhook")
                return {
                    "statusCode": 403,
                    "headers": {"Content-Type": "text/xml"},
                    "body": "<Response></Response>"
                }

            # Parse URL-encoded form data from Twilio
            from urllib.parse import parse_qs
            form_data = parse_qs(webhook_body)

            # Extract message details from Twilio webhook
            from_number = form_data.get('From', [''])[0]
            to_number = form_data.get('To', [''])[0]
            message_body = form_data.get('Body', [''])[0]
            twilio_sid = form_data.get('MessageSid', [''])[0]
            message_status = form_data.get('MessageStatus', [''])[0]

            # ── DELIVERY STATUS CALLBACK ─────────────────────────────────
            # Twilio POSTs sent/delivered/failed updates for outbound texts
            # (StatusCallback set by Send-SMS). These have MessageStatus but
            # no Body. ?msgRowId= identifies the messages row to update.
            if message_status and not message_body:
                status_map = {
                    "queued": "queued", "accepted": "queued", "sending": "sent",
                    "sent": "sent", "delivered": "delivered",
                    "undelivered": "failed", "failed": "failed",
                }
                new_status = status_map.get(message_status)
                qs = event.get("queryStringParameters") or {}
                row_id = qs.get("msgRowId")
                print(f"📬 Status callback: sid={twilio_sid} status={message_status} rowId={row_id}")
                if new_status:
                    try:
                        secret = get_secret()
                        conn = pymysql.connect(host=secret["host"], user=secret["user"],
                                               password=secret["password"], database=secret["database"],
                                               connect_timeout=5)
                        cursor = conn.cursor()
                        if row_id:
                            cursor.execute(
                                "UPDATE messages SET status=%s, twilio_sid=%s WHERE id=%s",
                                (new_status, twilio_sid or None, int(row_id)))
                        elif twilio_sid:
                            cursor.execute(
                                "UPDATE messages SET status=%s WHERE twilio_sid=%s",
                                (new_status, twilio_sid))
                        conn.commit()
                        cursor.close(); conn.close()
                        print(f"✅ Message status -> {new_status}")
                    except Exception as st_err:
                        print(f"⚠️ Status update failed: {st_err}")
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "text/xml"},
                    "body": "<Response></Response>"
                }

            print(f"📥 Inbound SMS from {from_number}: {message_body[:50]}...")

            if not from_number or not message_body:
                print("⚠️ Missing required fields in webhook")
                # Return 200 to Twilio even on error to prevent retries
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "text/xml"},
                    "body": "<Response></Response>"
                }

            # Store inbound message in database
            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Resolve tenant for this inbound webhook by matching the number
            # the customer texted (`To`) against each shop's Twilio number.
            # Unmatched -> tenant 1 (Brooklyn Bikery) for legacy behavior.
            inbound_tid = 1
            try:
                cursor.execute(
                    "SELECT id FROM tenants WHERE twilio_from_number = %s AND status='active'",
                    (to_number,))
                trow = cursor.fetchone()
                if trow:
                    inbound_tid = trow[0]
            except Exception as tr_err:
                print(f"⚠️ Inbound tenant match failed, defaulting to 1: {tr_err}")
            tenant = get_tenant(inbound_tid)

            # Use from_number as the phone (customer's number)
            cursor.execute("""
                INSERT INTO messages (tenant_id, phone, direction, body, status, twilio_sid, from_number, to_number)
                VALUES (%s, %s, 'inbound', %s, 'received', %s, %s, %s)
                ON DUPLICATE KEY UPDATE id=id
            """, (tenant["id"], from_number, message_body, twilio_sid, from_number, to_number))

            # ── STOP / START keyword handling (TCPA opt-out tracking) ────
            # Twilio blocks STOP'd numbers at the carrier level; we ALSO
            # record it so the app refuses to queue invoice texts and the
            # admin can see why. START/UNSTOP re-enables.
            keyword = message_body.strip().upper()
            digits10 = "".join(ch for ch in from_number if ch.isdigit())[-10:]
            try:
                if keyword in ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"):
                    cursor.execute(
                        "UPDATE customers SET sms_opted_out=1 WHERE tenant_id=%s AND "
                        "RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),10)=%s",
                        (tenant["id"], digits10))
                    print(f"⛔ Opt-out recorded for {from_number} (tenant {tenant['id']}, {cursor.rowcount} customer row(s))")
                elif keyword in ("START", "UNSTOP", "YES"):
                    cursor.execute(
                        "UPDATE customers SET sms_opted_out=0 WHERE tenant_id=%s AND "
                        "RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone,'+',''),'-',''),' ',''),'(',''),')',''),10)=%s",
                        (tenant["id"], digits10))
                    print(f"✅ Opt-in restored for {from_number} (tenant {tenant['id']}, {cursor.rowcount} customer row(s))")
            except Exception as opt_err:
                print(f"⚠️ Opt-out bookkeeping failed (non-fatal): {opt_err}")

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✅ Stored inbound message from {from_number}")

            # Send push notifications directly (Lambda timeout must be >= 30s)
            print(f"🔔 About to send push notifications...")
            try:
                send_push_notifications(from_number, message_body, secret, tenant["id"])
                print(f"🔔 Push notifications completed")
            except Exception as push_err:
                print(f"⚠️ Push notification error (non-fatal): {push_err}")
                traceback.print_exc()

            # Return empty TwiML response (no auto-reply)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/xml"},
                "body": "<Response></Response>"
            }

        except Exception as e:
            print(f"❌ Error in twilio-webhook: {str(e)}")
            traceback.print_exc()
            # Return 200 to Twilio even on error to prevent retries
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "text/xml"},
                "body": "<Response></Response>"
            }

    # ============================================
    # ALL OTHER ROUTES REQUIRE AUTHENTICATION
    # ============================================
    payload, error = require_auth(event)
    if error:
        print(f"❌ Auth failed: {error}")
        return response(401, {"error": error})

    # Multi-tenancy step 8: the tenant comes from the VERIFIED token (set at
    # login). Every data query below is scoped by this `tid` so one shop can
    # never see or touch another's rows. Pre-step-8 tokens lack the claim and
    # resolve to 1 (Brooklyn Bikery) — unchanged behavior.
    tid = resolve_tenant_id(payload=payload)
    print(f"✅ Authenticated admin request (tenant {tid})")
    
    # ============================================
    # SEND SMS ENDPOINT
    # Queues SMS message to S3 for processing by separate Lambda
    # ============================================
    is_send_sms = (
        '/send-sms' in raw_path or 
        '/send-sms' in path or
        '/send-sms' in resource or
        'send-sms' in route_key
    )
    
    if is_send_sms:
        try:
            print("📱 Handling /send-sms request")

            to_phone = body.get("to", "").strip()
            message_body = body.get("body", "").strip()
            order_id = body.get("orderId")
            media_url = body.get("mediaUrl", "").strip()  # Optional MMS image URL

            print(f"📞 Queueing SMS to {to_phone}, order_id={order_id}, has_media={bool(media_url)}")

            if not to_phone or not message_body:
                return response(400, {"message": "Phone number and message are required"})

            # Normalize phone to E.164 format for consistent storage
            phone_digits = ''.join(c for c in to_phone if c.isdigit())
            if len(phone_digits) == 10:
                to_phone_normalized = f"+1{phone_digits}"
            elif len(phone_digits) == 11 and phone_digits.startswith('1'):
                to_phone_normalized = f"+{phone_digits}"
            else:
                to_phone_normalized = to_phone  # Use as-is if already formatted

            # Resolve tenant config (creds for the job + from-number for the
            # history row). Tenant from the verified token.
            tenant = get_tenant(tid)
            from_number = tenant["twilio_from_number"]

            # Store outbound message FIRST so its row id can ride along in the
            # SMS job — Twilio's status callback then updates this exact row
            # (sent/delivered/failed).
            message_row_id = None
            try:
                secret = get_secret()
                conn = pymysql.connect(
                    host=secret["host"],
                    user=secret["user"],
                    password=secret["password"],
                    database=secret["database"],
                    connect_timeout=5,
                )
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO messages (tenant_id, phone, direction, body, status, from_number, to_number)
                    VALUES (%s, %s, 'outbound', %s, 'queued', %s, %s)
                """, (tenant["id"], to_phone_normalized, message_body, from_number, to_phone_normalized))
                message_row_id = cursor.lastrowid
                conn.commit()
                cursor.close()
                conn.close()
                print(f"📝 Message stored in database for {to_phone_normalized} (row {message_row_id})")
            except Exception as db_err:
                # Don't fail the SMS queue if database insert fails
                print(f"⚠️ Failed to store message in database: {db_err}")

            # Upload SMS/MMS job to S3 for processing. The job carries the
            # tenant's Twilio credentials (SendSMS is DB-less): previously this
            # path omitted them, silently sending every shop's dashboard texts
            # through Brooklyn Bikery's legacy Twilio account.
            s3_client = boto3.client('s3')
            sms_bucket = os.getenv("SMS_BUCKET", "brooklyn-bikery-sms")

            sms_job = {
                "to": to_phone_normalized,
                "body": message_body,
                "tenant_id": tenant["id"],
                "twilio_account_sid": tenant["twilio_account_sid"],
                "twilio_auth_token_secret_arn": tenant["twilio_auth_token_secret_arn"],
                "twilio_from_number": from_number,
                "message_row_id": message_row_id,
            }

            # Add media URL for MMS if provided
            if media_url:
                sms_job["mediaUrl"] = media_url

            # Create unique job filename
            job_key = f"sms/admin_msg_{order_id or 'manual'}_{uuid.uuid4().hex[:8]}.json"

            s3_client.put_object(
                Bucket=sms_bucket,
                Key=job_key,
                Body=json.dumps(sms_job),
                ContentType='application/json'
            )

            print(f"✅ {'MMS' if media_url else 'SMS'} queued successfully: {job_key}")

            return response(200, {
                "message": f"{'MMS' if media_url else 'SMS'} queued successfully",
                "job_key": job_key
            })

        except Exception as e:
            print(f"❌ Error in send-sms: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})
    
    # ============================================
    # GET UPLOAD URL ENDPOINT
    # Returns a presigned S3 URL for uploading MMS images
    # ============================================
    is_get_upload_url = body.get("action") == "get-upload-url"

    if is_get_upload_url:
        try:
            print("📤 Handling get-upload-url request")

            file_name = body.get("fileName", "")
            file_type = body.get("fileType", "")

            if not file_name or not file_type:
                return response(400, {"message": "fileName and fileType are required"})

            # Validate file type (only images allowed)
            allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
            if file_type not in allowed_types:
                return response(400, {"message": "Only image files (JPG, PNG, GIF, WebP) are allowed"})

            # Generate unique key for the image
            from botocore.config import Config
            s3_client = boto3.client('s3', region_name=REGION, config=Config(signature_version='s3v4'))
            mms_bucket = os.getenv("SMS_BUCKET", "brooklyn-bikery-sms")  # same bucket as SMS jobs, different prefix
            timestamp = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d_%H%M%S")
            safe_filename = "".join(c for c in file_name if c.isalnum() or c in '._-')[:50]
            image_key = f"mms-images/{timestamp}_{uuid.uuid4().hex[:8]}_{safe_filename}"

            # Generate presigned URL for upload — pin Content-Type to the validated
            # type and require it on the PUT (browser must send the matching header).
            # The 10MB cap is enforced by the bucket policy; we also reject obvious
            # oversize uploads here by including the X-Amz-Server-Side-Encryption
            # constraint isn't applicable to PUT presigns, so the bucket policy +
            # content-type pinning are our two layers.
            presigned_url = s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': mms_bucket,
                    'Key': image_key,
                    'ContentType': file_type,  # browser MUST send matching Content-Type
                },
                ExpiresIn=300  # 5 minutes
            )

            # Public URL (bucket is public-read on mms-images/* via bucket policy)
            public_url = f"https://{mms_bucket}.s3.amazonaws.com/{image_key}"

            print(f"✅ Generated upload URL for: {image_key} (type={file_type})")

            return response(200, {
                "uploadUrl": presigned_url,
                "publicUrl": public_url,
                "key": image_key,
                "requiredContentType": file_type
            })

        except Exception as e:
            print(f"❌ Error in get-upload-url: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # GET DATABASE TABLES ENDPOINT
    # Returns all rows from customers and orders for the DB viewer tab
    # ============================================
    is_get_db = (
        '/get-db-tables' in raw_path or
        '/get-db-tables' in path or
        '/get-db-tables' in resource or
        'get-db-tables' in route_key or
        body.get("action") == "get-db-tables"
    )

    if is_get_db:
        try:
            print("🗄️ Handling get-db-tables request")

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Customers (tenant-scoped)
            cursor.execute("SELECT * FROM customers WHERE tenant_id = %s ORDER BY id DESC", (tid,))
            cust_cols = [desc[0] for desc in cursor.description]
            customers = [dict(zip(cust_cols, row)) for row in cursor.fetchall()]
            for r in customers:
                if r.get('date_created'):
                    r['date_created'] = str(r['date_created'])

            # Orders (tenant-scoped)
            from decimal import Decimal
            cursor.execute("SELECT * FROM orders WHERE tenant_id = %s ORDER BY id ASC", (tid,))
            ord_cols = [desc[0] for desc in cursor.description]
            orders = []
            for row in cursor.fetchall():
                d = dict(zip(ord_cols, row))
                if d.get('date_of_service'):
                    d['date_of_service'] = str(d['date_of_service'])
                for k, v in list(d.items()):
                    if isinstance(v, Decimal):
                        d[k] = float(v) if v is not None else None
                orders.append(d)

            cursor.close()
            conn.close()

            print(f"✅ Returned {len(customers)} customers, {len(orders)} orders")
            return response(200, {
                "customers": {"columns": cust_cols, "rows": customers},
                "orders": {"columns": ord_cols, "rows": orders}
            })

        except Exception as e:
            print(f"❌ Error in get-db-tables: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # GET MESSAGES ENDPOINT
    # Returns conversation history for a phone number
    # ============================================
    is_get_messages = (
        '/get-messages' in raw_path or
        '/get-messages' in path or
        '/get-messages' in resource or
        'get-messages' in route_key or
        body.get("action") == "get-messages"
    )

    if is_get_messages:
        try:
            print("💬 Handling get-messages request")

            phone = body.get("phone", "").strip()
            limit = int(body.get("limit", 50))  # Default to last 50 messages
            before_id = body.get("before_id")   # For pagination: load messages older than this ID

            if not phone:
                return response(400, {"message": "Phone number is required"})

            # Normalize phone to E.164 format for consistent matching
            phone_digits = ''.join(c for c in phone if c.isdigit())
            if len(phone_digits) == 10:
                phone_normalized = f"+1{phone_digits}"
            elif len(phone_digits) == 11 and phone_digits.startswith('1'):
                phone_normalized = f"+{phone_digits}"
            else:
                phone_normalized = phone  # Use as-is if already formatted or international

            print(f"📱 Looking up messages for: {phone} -> {phone_normalized}, before_id={before_id}")

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Fetch limit+1 rows so we can detect if there are more pages
            fetch_limit = limit + 1

            if before_id:
                # Paginated: load messages older than before_id
                cursor.execute("""
                    SELECT id, phone, direction, body, status, twilio_sid,
                           from_number, to_number, created_at
                    FROM messages
                    WHERE tenant_id = %s AND (phone = %s OR phone = %s) AND id < %s
                    ORDER BY id DESC
                    LIMIT %s
                """, (tid, phone, phone_normalized, int(before_id), fetch_limit))
            else:
                # Initial load: most recent messages
                cursor.execute("""
                    SELECT id, phone, direction, body, status, twilio_sid,
                           from_number, to_number, created_at
                    FROM messages
                    WHERE tenant_id = %s AND (phone = %s OR phone = %s)
                    ORDER BY id DESC
                    LIMIT %s
                """, (tid, phone, phone_normalized, fetch_limit))

            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]  # trim the sentinel row

            messages = []
            for row in rows:
                msg = dict(zip(columns, row))
                if msg.get('created_at'):
                    msg['created_at'] = str(msg['created_at'])
                messages.append(msg)

            # Reverse to show oldest first in UI
            messages.reverse()

            cursor.close()
            conn.close()

            print(f"✅ Returned {len(messages)} messages for {phone}, has_more={has_more}")
            return response(200, {"messages": messages, "has_more": has_more})

        except Exception as e:
            print(f"❌ Error in get-messages: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # GET MESSAGE THREADS ENDPOINT
    # Returns list of unique phone numbers with message count and last message
    # ============================================
    is_get_threads = (
        '/get-message-threads' in raw_path or
        '/get-message-threads' in path or
        '/get-message-threads' in resource or
        'get-message-threads' in route_key or
        body.get("action") == "get-message-threads"
    )

    if is_get_threads:
        try:
            print("📋 Handling get-message-threads request")

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Get unique phone numbers with their last message and count
            # Join with customers using normalized phone (strip +1 from messages.phone to match customers.phone)
            cursor.execute("""
                SELECT
                    m.phone,
                    c.name as customer_name,
                    COUNT(*) as message_count,
                    MAX(m.created_at) as last_message_at,
                    (SELECT body FROM messages m2 WHERE m2.phone = m.phone AND m2.tenant_id = %s ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1) as last_message,
                    (SELECT direction FROM messages m2 WHERE m2.phone = m.phone AND m2.tenant_id = %s ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1) as last_message_direction
                FROM messages m
                LEFT JOIN customers c ON
                    RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(c.phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) =
                    RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(m.phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10)
                    AND c.tenant_id = %s
                WHERE m.tenant_id = %s
                GROUP BY m.phone, c.name
                ORDER BY last_message_at DESC
            """, (tid, tid, tid, tid))

            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            threads = []
            for row in rows:
                thread = dict(zip(columns, row))
                if thread.get('last_message_at'):
                    thread['last_message_at'] = str(thread['last_message_at'])
                threads.append(thread)

            cursor.close()
            conn.close()

            print(f"✅ Returned {len(threads)} message threads")
            return response(200, {"threads": threads})

        except Exception as e:
            print(f"❌ Error in get-message-threads: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # GET VAPID PUBLIC KEY ENDPOINT
    # Returns public key for push notification subscription
    # ============================================
    is_get_vapid = body.get("action") == "get-vapid-key"

    if is_get_vapid:
        try:
            print("🔑 Handling get-vapid-key request")
            client = boto3.client("secretsmanager", region_name=REGION)
            resp = client.get_secret_value(SecretId="bikery-vapid-keys")
            vapid_keys = json.loads(resp["SecretString"])
            return response(200, {"publicKey": vapid_keys["publicKey"]})
        except Exception as e:
            print(f"❌ Error in get-vapid-key: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # SAVE PUSH SUBSCRIPTION ENDPOINT
    # Stores push subscription in database
    # ============================================
    is_save_subscription = body.get("action") == "save-push-subscription"

    if is_save_subscription:
        try:
            print("🔔 Handling save-push-subscription request")

            subscription = body.get("subscription", {})
            endpoint = subscription.get("endpoint", "")
            keys = subscription.get("keys", {})
            p256dh = keys.get("p256dh", "")
            auth = keys.get("auth", "")
            user_agent = body.get("userAgent", "")

            if not endpoint or not p256dh or not auth:
                return response(400, {"message": "Invalid subscription data"})

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Upsert subscription (insert or update if endpoint exists).
            # tenant-scoped: a device's subscription belongs to the logged-in shop.
            cursor.execute("""
                INSERT INTO push_subscriptions (tenant_id, endpoint, p256dh, auth, user_agent)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    tenant_id = VALUES(tenant_id),
                    p256dh = VALUES(p256dh),
                    auth = VALUES(auth),
                    user_agent = VALUES(user_agent),
                    created_at = CURRENT_TIMESTAMP
            """, (tid, endpoint, p256dh, auth, user_agent))

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✅ Saved push subscription")
            return response(200, {"message": "Subscription saved"})

        except Exception as e:
            print(f"❌ Error in save-push-subscription: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # DELETE PUSH SUBSCRIPTION ENDPOINT
    # Removes push subscription from database
    # ============================================
    is_delete_subscription = body.get("action") == "delete-push-subscription"

    if is_delete_subscription:
        try:
            print("🔕 Handling delete-push-subscription request")

            endpoint = body.get("endpoint", "")

            if not endpoint:
                return response(400, {"message": "Endpoint is required"})

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            cursor.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✅ Deleted push subscription")
            return response(200, {"message": "Subscription deleted"})

        except Exception as e:
            print(f"❌ Error in delete-push-subscription: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # SERVICE CATALOG ENDPOINTS (per-tenant service & price editor)
    # catalog-list: every catalog row for this tenant (incl. inactive) for
    #               the dashboard's Services editor.
    # catalog-save: update one row (name/price/category/active/sort) or add a
    #               new fixed-price service. Codes and formulas of existing
    #               rows are immutable (order_services history references
    #               them); rows are deactivated, never deleted.
    # ============================================
    if body.get("action") == "catalog-list":
        try:
            secret = get_secret()
            conn = pymysql.connect(host=secret["host"], user=secret["user"],
                                   password=secret["password"], database=secret["database"],
                                   connect_timeout=5, charset="utf8mb4")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, code, display_name, default_price, pricing_formula, "
                "category, is_active, sort_order FROM service_catalog "
                "WHERE tenant_id = %s ORDER BY sort_order, id", (tid,))
            services = [
                {"id": r[0], "code": r[1], "name": r[2],
                 "price": float(r[3]) if r[3] is not None else None,
                 "formula": r[4], "category": r[5] or "other",
                 "active": bool(r[6]), "sort": r[7]}
                for r in cursor.fetchall()
            ]
            cursor.close(); conn.close()
            return response(200, {"services": services})
        except Exception as e:
            print(f"❌ Error in catalog-list: {e}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    if body.get("action") == "catalog-save":
        try:
            svc = body.get("service") or {}

            def _bad(msg):
                return response(400, {"message": msg})

            name = (svc.get("name") or "").strip()
            category = (svc.get("category") or "other").strip().lower()[:50] or "other"
            active = 1 if svc.get("active", True) else 0
            price_raw = svc.get("price")
            try:
                price = round(float(price_raw), 2) if price_raw is not None else None
            except (TypeError, ValueError):
                return _bad("Price must be a number")
            try:
                sort = int(svc.get("sort") or 0)
            except (TypeError, ValueError):
                sort = 0

            secret = get_secret()
            conn = pymysql.connect(host=secret["host"], user=secret["user"],
                                   password=secret["password"], database=secret["database"],
                                   connect_timeout=5, charset="utf8mb4")
            cursor = conn.cursor()
            try:
                if svc.get("id"):
                    # ── Update an existing row (tenant-scoped) ──────────────
                    sid = int(svc["id"])
                    cursor.execute(
                        "SELECT pricing_formula FROM service_catalog "
                        "WHERE id = %s AND tenant_id = %s", (sid, tid))
                    row = cursor.fetchone()
                    if not row:
                        return response(404, {"message": "Service not found"})
                    formula = row[0]
                    if not name:
                        return _bad("Name is required")
                    if formula == "fixed" and (price is None or price < 0 or price > 99999):
                        return _bad("Price must be between 0 and 99999")
                    cursor.execute(
                        "UPDATE service_catalog SET display_name=%s, "
                        "default_price=COALESCE(%s, default_price), category=%s, "
                        "is_active=%s, sort_order=%s "
                        "WHERE id=%s AND tenant_id=%s",
                        (name, price, category, active, sort, sid, tid))
                else:
                    # ── Add a new fixed-price service ───────────────────────
                    if not name:
                        return _bad("Name is required")
                    if price is None or price < 0 or price > 99999:
                        return _bad("Price must be between 0 and 99999")
                    # Generate a stable, unique, URL/DB-safe code from the name.
                    base = "".join(c if c.isalnum() else "_" for c in name.lower())
                    base = "_".join(filter(None, base.split("_")))[:40] or "service"
                    code = base
                    n = 2
                    while True:
                        cursor.execute(
                            "SELECT 1 FROM service_catalog WHERE tenant_id=%s AND code=%s",
                            (tid, code))
                        if not cursor.fetchone():
                            break
                        code = f"{base}_{n}"[:48]
                        n += 1
                    cursor.execute(
                        "INSERT INTO service_catalog (tenant_id, code, display_name, "
                        "default_price, pricing_formula, category, is_active, sort_order) "
                        "VALUES (%s, %s, %s, %s, 'fixed', %s, %s, %s)",
                        (tid, code, name, price, category, active, sort))
                conn.commit()
            finally:
                cursor.close(); conn.close()
            return response(200, {"message": "Saved"})
        except Exception as e:
            print(f"❌ Error in catalog-save: {e}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # DATA EXPORT ENDPOINT (per-tenant CSV)
    # "Your data is yours, exportable" — returns this tenant's customers,
    # orders (with service line items), and message history as CSV text.
    # ============================================
    if body.get("action") == "export-data":
        try:
            import csv as _csv
            import io as _io

            def _rows_to_csv(header, rows):
                buf = _io.StringIO()
                w = _csv.writer(buf)
                w.writerow(header)
                w.writerows(rows)
                return buf.getvalue()

            secret = get_secret()
            conn = pymysql.connect(host=secret["host"], user=secret["user"],
                                   password=secret["password"], database=secret["database"],
                                   connect_timeout=5, charset="utf8mb4")
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, name, phone, date_created, sms_consent, sms_opted_out "
                "FROM customers WHERE tenant_id = %s ORDER BY id", (tid,))
            customers_csv = _rows_to_csv(
                ["id", "name", "phone", "date_created", "sms_consent", "sms_opted_out"],
                cursor.fetchall())

            cursor.execute("""
                SELECT o.id, o.date_of_service, c.name, c.phone, o.bike_description,
                       o.backend_notes, o.price, o.final_price,
                       COALESCE((SELECT GROUP_CONCAT(CONCAT(sc.display_name,
                                CASE WHEN os.quantity > 1 THEN CONCAT(' x', os.quantity) ELSE '' END,
                                ' ($', os.price_charged, ')') SEPARATOR '; ')
                         FROM order_services os JOIN service_catalog sc ON sc.id = os.service_catalog_id
                         WHERE os.order_id = o.id AND os.tenant_id = o.tenant_id), '') AS services
                FROM orders o JOIN customers c ON c.id = o.customer_id AND c.tenant_id = o.tenant_id
                WHERE o.tenant_id = %s ORDER BY o.id""", (tid,))
            orders_csv = _rows_to_csv(
                ["order_id", "date_of_service", "customer_name", "customer_phone",
                 "bike_description", "notes", "subtotal", "total_with_tax", "services"],
                cursor.fetchall())

            cursor.execute(
                "SELECT id, created_at, direction, phone, status, body "
                "FROM messages WHERE tenant_id = %s ORDER BY id", (tid,))
            messages_csv = _rows_to_csv(
                ["id", "created_at", "direction", "phone", "status", "body"],
                cursor.fetchall())

            cursor.close(); conn.close()
            print(f"✅ export-data: tenant {tid} export generated")
            return response(200, {
                "customers": customers_csv,
                "orders": orders_csv,
                "messages": messages_csv,
            })
        except Exception as e:
            print(f"❌ Error in export-data: {e}")
            traceback.print_exc()
            return response(500, {"message": "Export failed"})

    # ============================================
    # GET CUSTOMERS ENDPOINT
    # Returns all customers for mass SMS selection UI
    # ============================================
    is_get_customers = (
        '/get-customers' in raw_path or
        '/get-customers' in path or
        '/get-customers' in resource or
        'get-customers' in route_key or
        body.get("action") == "get-customers"
    )

    if is_get_customers:
        try:
            print("👥 Handling get-customers request")

            secret = get_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()

            # Simple query — just what the mass SMS UI needs (tenant-scoped)
            cursor.execute("SELECT id, name, phone FROM customers WHERE tenant_id = %s ORDER BY id DESC", (tid,))
            rows = cursor.fetchall()

            customers = [{"id": r[0], "name": r[1], "phone": r[2]} for r in rows]

            cursor.close()
            conn.close()

            print(f"✅ Returned {len(customers)} customers")
            return response(200, {"customers": customers})

        except Exception as e:
            print(f"❌ Error in get-customers: {str(e)}")
            traceback.print_exc()
            return response(500, {"message": "Internal error"})

    # ============================================
    # SEARCH ORDERS ENDPOINT (Default route)
    # Returns orders filtered by date range and/or customer info
    # ============================================
    try:
        print("📝 Handling search request")
        
        # Extract search parameters
        start_date = body.get("startDate")
        end_date = body.get("endDate") 
        customer_name = body.get("customerName")
        customer_phone = body.get("customerPhone")
        
        print(f"🔍 Search params: startDate={start_date}, endDate={end_date}, name={customer_name}, phone={customer_phone}")

        # Connect to database
        secret = get_secret()
        conn = pymysql.connect(
            host=secret["host"],
            user=secret["user"],
            password=secret["password"],
            database=secret["database"],
            connect_timeout=5,
        )
        cursor = conn.cursor()

        # Build query joining orders and customers.
        # Service columns (booleans, spoke counts, custom_service trio) are NOT
        # selected here — they're synthesized from `order_services` after the
        # main fetch via synthesize_order_services(). This is step 5 of the
        # multi-tenant migration: reads come from the new line-item table while
        # the legacy boolean columns remain populated for safety until step 6.
        base_query = """
        SELECT
            o.id,
            o.customer_id,
            o.date_of_service,
            o.bike_description,
            o.customer_notes,
            o.backend_notes,
            c.name as customer_name,
            c.phone as customer_phone,
            o.price,
            o.final_price
        FROM orders o
        JOIN customers c ON o.customer_id = c.id AND c.tenant_id = o.tenant_id
        WHERE 1=1 AND o.tenant_id = %s
        """

        params = [tid]
        
        # Build dynamic WHERE clause based on provided filters
        if start_date:
            base_query += " AND o.date_of_service >= %s"
            params.append(start_date)
        
        if end_date:
            base_query += " AND o.date_of_service <= %s"
            params.append(end_date)
        
        if customer_name:
            base_query += " AND c.name LIKE %s"
            params.append(f"%{customer_name}%")
        
        if customer_phone:
            base_query += " AND c.phone LIKE %s"
            params.append(f"%{customer_phone}%")
        
        # Order by most recent first
        base_query += " ORDER BY o.date_of_service DESC, o.id DESC"

        cursor.execute(base_query, params)
        results = cursor.fetchall()
        print(f"📊 Query returned {len(results)} rows")
        
        # Convert results to dictionary format
        columns = [desc[0] for desc in cursor.description]
        
        orders = []
        from decimal import Decimal
        for row in results:
            order_dict = dict(zip(columns, row))

            # Convert date objects to strings for JSON serialization
            if order_dict.get('date_of_service'):
                order_dict['date_of_service'] = str(order_dict['date_of_service'])

            # Convert Decimal types to float for JSON serialization
            for key, value in list(order_dict.items()):
                if isinstance(value, Decimal):
                    order_dict[key] = float(value) if value is not None else None

            orders.append(order_dict)

        # Merge synthesized service data from order_services (step 5 of the
        # multi-tenant migration). One batch query for every order returned,
        # then per-order merge into the dict. Frontend sees the same shape
        # the legacy SELECT used to produce, but the data now comes from
        # the new line-item table. tenant_id hardcoded to 1 until step 8
        # wires up URL-based tenant resolution.
        if orders:
            try:
                order_ids = [o['id'] for o in orders]
                service_data = synthesize_order_services(cursor, tid, order_ids)
                for o in orders:
                    o.update(service_data.get(o['id'], {}))
            except Exception as synth_err:
                # Fail loud in logs but don't break the search endpoint — the
                # response will be missing service fields, frontend will show
                # empty services. Better than 500ing the whole admin dashboard.
                print(f"⚠️ synthesize_order_services failed: {synth_err}")
                traceback.print_exc()

        cursor.close()
        conn.close()

        return response(200, {
            "message": "Success",
            "orders": orders,
            "total_count": len(orders)
        })

    except Exception as e:
        print(f"❌ Error in search: {str(e)}")
        traceback.print_exc()
        return response(500, {"message": "Internal error"})