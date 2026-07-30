"""
Customer Intake Lambda Function
Handles new customer submissions from the Brooklyn Bikery intake form.
Validates input, checks rate limits, and creates customer/order records in MySQL.
"""

import json
import pymysql
import boto3
import base64
import os
import re
from urllib.parse import parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo
from botocore.config import Config

# Environment configuration
REGION = os.getenv("AWS_REGION", "us-east-1")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://brooklynbikery.com")

# Secrets Manager endpoint (optional override for testing)
SM_ENDPOINT = os.getenv("SM_ENDPOINT")

# Boto3 client configuration with strict timeouts
BOTO_CFG = Config(read_timeout=3, connect_timeout=3, retries={'max_attempts': 1})

# In-memory cache for rate limiting (resets when Lambda cold-starts)
request_cache = {}

# ── Tenant resolution (+ best-effort CORS reflection) ──────────────────────
# The public intake form is served from each shop's own origin (its
# {slug}.bluewrenchhq.com, or brooklynbikery.com). We resolve WHICH tenant a
# submission belongs to from that Origin, so a second shop's customers and
# orders are created under THAT shop — without this the tenant_id column
# default (1) would silently misfile every other shop's intake under Brooklyn
# Bikery. (Browser CORS is enforced by the API Gateway's own CorsConfiguration,
# which ignores these Lambda headers; provision_tenant.py registers each shop's
# origin there. The reflected header below is harmless defense-in-depth.)
_TENANT_ORIGIN_CACHE = {"rows": None, "loaded_at": 0.0}
# Cache TTL for the tenant→origin map. Prod default 300s; staging sets a small
# value so integration tests see origin changes without a long wait.
_TENANT_ORIGIN_TTL = int(os.getenv("TENANT_ORIGIN_TTL", "300"))

# Per-invocation resolved values (one request per warm container at a time).
_REQUEST_ORIGIN = ALLOWED_ORIGIN
_REQUEST_TENANT_ID = 1


def _tenant_origin_rows():
    """[(tenant_id, normalized_allowed_origin, slug), ...] for active tenants, cached."""
    now = datetime.now().timestamp()
    cached = _TENANT_ORIGIN_CACHE["rows"]
    if cached is not None and now - _TENANT_ORIGIN_CACHE["loaded_at"] < _TENANT_ORIGIN_TTL:
        return cached
    rows = []
    try:
        secret = get_secret()
        conn = pymysql.connect(host=secret["host"], user=secret["user"],
                               password=secret["password"], database=secret["database"],
                               port=int(secret.get("port", 3306)), connect_timeout=3)
        try:
            with conn.cursor() as cur:
                # ORDER BY id makes tie-breaks deterministic: if two active
                # tenants somehow share an origin, the lowest id (the primary
                # shop) wins rather than a nondeterministic row order.
                cur.execute("SELECT id, allowed_origin, slug FROM tenants "
                            "WHERE status='active' ORDER BY id")
                for tid, origin, slug in cur.fetchall():
                    rows.append((tid, (origin or "").strip().rstrip("/"), (slug or "").strip().lower()))
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️ tenant load failed, using cached/default: {e}")
        if cached is not None:
            return cached
    _TENANT_ORIGIN_CACHE["rows"] = rows
    _TENANT_ORIGIN_CACHE["loaded_at"] = now
    return rows


def resolve_request(event):
    """Set the reflected CORS origin and the tenant for THIS request (by Origin)."""
    global _REQUEST_ORIGIN, _REQUEST_TENANT_ID
    headers = event.get("headers") or {}
    origin = (headers.get("origin") or headers.get("Origin") or "").strip().rstrip("/")
    rows = _tenant_origin_rows()
    allowed = {ALLOWED_ORIGIN} | {o for _, o, _ in rows if o}
    _REQUEST_ORIGIN = origin if origin and origin in allowed else ALLOWED_ORIGIN
    _REQUEST_TENANT_ID = 1
    if origin:
        for tid, o, _slug in rows:
            if o and o == origin:
                _REQUEST_TENANT_ID = tid
                break


def tenant_id_for_slug(slug):
    """Resolve an active tenant id from a shop slug (the ?tenant= override on the
    shared host). Returns None if the slug matches no active tenant."""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    for tid, _o, s in _tenant_origin_rows():
        if s and s == slug:
            return tid
    return None


def cors_headers():
    return {
        "Access-Control-Allow-Origin": _REQUEST_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Vary": "Origin",
    }


def response(status, body):
    """Helper to format API Gateway response with CORS headers"""
    return {"statusCode": status, "headers": cors_headers(), "body": json.dumps(body)}

def get_secret():
    """
    Retrieve database credentials from AWS Secrets Manager.
    Raises RuntimeError if credentials cannot be fetched.
    """
    try:
        client = boto3.client(
            "secretsmanager",
            region_name=REGION,
            endpoint_url=SM_ENDPOINT if SM_ENDPOINT else None,
            config=BOTO_CFG
        )
        out = client.get_secret_value(SecretId=os.getenv("DB_SECRET_ID", "bikeshop-credentials"))
        return json.loads(out["SecretString"])
    except Exception as e:
        raise RuntimeError(f"Configuration error")

def validate_phone(phone):
    """
    Validate and normalize US phone number.
    Accepts formats like: (555) 123-4567, 555-123-4567, 5551234567
    Returns: 10-digit string or False if invalid
    """
    if not phone:
        return False
    
    # Remove common formatting characters
    cleaned = re.sub(r'[\s\-\(\)\.]+', '', phone)
    
    # Accept 10 or 11 digits (with leading 1)
    if re.match(r'^1?\d{10}$', cleaned):
        return cleaned[-10:]  # Return last 10 digits
    
    return False

def validate_input(data):
    """
    Validate all form inputs before database insertion.
    Returns: (validated_data, None) on success or (None, errors) on failure
    """
    errors = []
    
    # Name validation
    name = data.get("name", "").strip()
    if not name or len(name) < 2:
        errors.append("Name is required and must be at least 2 characters")
    if len(name) > 100:
        errors.append("Name must be less than 100 characters")
    
    # Phone validation
    phone = validate_phone(data.get("phone", ""))
    if not phone:
        errors.append("Valid 10-digit phone number is required")
    
    # Notes validation (optional field)
    notes = data.get("notes", "").strip()
    if len(notes) > 1000:
        errors.append("Notes must be less than 1000 characters")
    
    # Consent checkboxes (required)
    if data.get("serviceConsent") != "on":
        errors.append("Service consent is required")
    if data.get("marketingConsent") != "on":
        errors.append("Marketing consent is required")
    
    if errors:
        return None, errors
    
    return {
        "name": name[:100],
        "phone": phone,
        "notes": notes[:1000]
    }, None

def check_rate_limit(phone):
    """
    Simple rate limiting: 3 submissions per phone number per hour.
    Uses in-memory cache (resets on Lambda cold start).
    Returns: True if request allowed, False if rate limit exceeded
    """
    now = datetime.now()
    cache_key = f"phone_{phone}"
    
    if cache_key in request_cache:
        last_time, count = request_cache[cache_key]
        
        # Reset counter after 1 hour
        if (now - last_time).total_seconds() > 3600:
            request_cache[cache_key] = (now, 1)
            return True
        
        # Block if limit exceeded
        if count >= 3:
            return False
        
        # Increment counter
        request_cache[cache_key] = (last_time, count + 1)
        return True
    
    # First request from this phone
    request_cache[cache_key] = (now, 1)
    return True

def lambda_handler(event, context):
    """
    Main handler for customer intake form submissions.
    
    Process flow:
    1. Handle CORS preflight requests
    2. Decode and parse form data
    3. Check honeypot for bot detection
    4. Validate all inputs
    5. Apply rate limiting
    6. Find or create customer record
    7. Create new order record
    8. Return success response
    """
    try:
        # Resolve the reflected CORS origin + which tenant this submission
        # belongs to (by Origin) before doing anything else.
        resolve_request(event)

        # Handle CORS preflight OPTIONS request
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {"ok": True})

        # Decode request body (may be base64-encoded by API Gateway)
        raw_body = event.get("body", "") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        
        # Parse URL-encoded form data
        data = {k: v[0] for k, v in parse_qs(raw_body).items()}
        
        # Honeypot bot detection (hidden field that humans won't fill)
        if data.get("website"):
            print(f"Bot detected - honeypot triggered")
            return response(400, {"error": "Invalid submission"})
        
        # Validate all inputs
        validated_data, errors = validate_input(data)
        if errors:
            return response(400, {"error": "Validation failed", "details": errors})
        
        # Check rate limit
        if not check_rate_limit(validated_data["phone"]):
            return response(429, {"error": "Too many requests. Please try again later."})
        
        # Get current date in NYC timezone
        today = str(datetime.now(ZoneInfo("America/New_York")).date())
        
        # Retrieve database credentials from Secrets Manager
        try:
            secret = get_secret()
        except Exception as e:
            print(f"Secrets error: {e}")
            return response(500, {"error": "Service temporarily unavailable"})
        
        # Connect to MySQL database
        try:
            connection = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                port=int(secret.get("port", 3306)),
                connect_timeout=5,
                autocommit=False  # Manual commit for transaction control
            )
        except Exception as e:
            print(f"DB connection error: {e}")
            return response(500, {"error": "Service temporarily unavailable"})
        
        # Tenant precedence: the Origin (browser-enforced, primary path) wins;
        # if the Origin didn't pin a specific shop (shared host -> tenant 1), a
        # ?tenant= slug in the form may select an active shop. The slug is public
        # (it's in the shop's URL) and submitting intake to a shop is not
        # sensitive, so this is safe.
        tenant_id = _REQUEST_TENANT_ID
        if tenant_id == 1:
            slug_tid = tenant_id_for_slug(data.get("tenant"))
            if slug_tid:
                tenant_id = slug_tid
        try:
            with connection.cursor() as cursor:
                # Check if customer already exists by phone — SCOPED to this
                # tenant (phone is unique per tenant, not globally), so shops
                # that happen to share a customer phone stay isolated.
                cursor.execute(
                    "SELECT id FROM customers WHERE phone = %s AND tenant_id = %s",
                    (validated_data["phone"], tenant_id))
                row = cursor.fetchone()

                if row:
                    # Use existing customer ID
                    customer_id = row[0]
                    # Note: We preserve the original customer name and don't update it
                    # Name updates should only happen through the admin backend
                else:
                    # Create new customer record. serviceConsent was required
                    # and validated above, so record SMS consent with a
                    # timestamp (TCPA record-keeping).
                    cursor.execute("""
                        INSERT INTO customers (tenant_id, name, phone, date_created, sms_consent, sms_consent_at)
                        VALUES (%s, %s, %s, %s, 1, NOW())
                    """, (
                        tenant_id,
                        validated_data["name"],
                        validated_data["phone"],
                        today
                    ))
                    customer_id = cursor.lastrowid

                # Create new order for this service request (tenant-scoped)
                cursor.execute("""
                    INSERT INTO orders (tenant_id, customer_id, date_of_service, customer_notes)
                    VALUES (%s, %s, %s, %s)
                """, (tenant_id, customer_id, today, validated_data["notes"]))
                order_id = cursor.lastrowid

                # Commit transaction
                connection.commit()

        except Exception as e:
            print(f"DB query error: {e}")
            try:
                connection.rollback()
            except Exception:
                pass
            return response(500, {"error": "Service temporarily unavailable"})
        finally:
            try:
                connection.close()
            except Exception:
                pass

        # Success response
        return response(200, {"message": "Success", "order_id": order_id})
        
    except Exception as e:
        print(f"Unhandled error: {e}")
        return response(500, {"error": "Service temporarily unavailable"})