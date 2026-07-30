"""
Backend Service Submission Lambda
Handles admin form submissions for adding services to existing customer orders.
Requires JWT authentication, updates order records, and queues SMS invoice.
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

# tenant_id is hardcoded to 1 (Brooklyn Bikery) until the multi-tenancy
# migration (step 8) wires up tenant resolution from the request URL.
# Module-level so the edit/lookup path and the order_services writer both
# reference the same constant (a local assignment would shadow it and break
# earlier references with UnboundLocalError).
TENANT_ID = 1

# ── Multi-origin CORS ────────────────────────────────────────────────────────
# Each tenant can serve its admin pages from its own origin (future
# {shop}.bluewrenchhq.com). The allow-list is: the env-var origin (prod or
# staging default) + every active tenant's `allowed_origin`. The request's
# Origin header is reflected back ONLY if it's in that list; anything else
# gets the env default (which the browser then correctly blocks).
_ORIGIN_CACHE = {"origins": None, "loaded_at": 0.0}
_ORIGIN_TTL_SECONDS = 300

# Per-invocation resolved origin. Lambda handles one request per container at
# a time, so a module global is safe here.
_REQUEST_ORIGIN = ALLOWED_ORIGIN


def _allowed_origins():
    """Set of origins allowed to call this API (env default + per-tenant)."""
    now = time.time()
    if _ORIGIN_CACHE["origins"] is not None and now - _ORIGIN_CACHE["loaded_at"] < _ORIGIN_TTL_SECONDS:
        return _ORIGIN_CACHE["origins"]
    origins = {ALLOWED_ORIGIN}
    try:
        secret = get_db_secret()
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
        # DB hiccup: fall back to whatever we had (env default at minimum).
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
# JWT VERIFICATION
# ============================================
def base64url_decode(data):
    """
    Base64 URL-safe decoding with automatic padding restoration.
    JWT tokens use base64url encoding without padding.
    """
    padding = 4 - len(data) % 4
    if padding != 4:
        data += '=' * padding
    return base64.urlsafe_b64decode(data)

def get_jwt_secret():
    """
    Retrieve JWT signing secret from AWS Secrets Manager.
    Returns: Secret string or None on failure
    """
    client = boto3.client("secretsmanager", region_name=REGION)
    try:
        resp = client.get_secret_value(SecretId=os.getenv("JWT_SECRET_ID", "bikery-jwt-secret"))
        secret_data = json.loads(resp["SecretString"])
        return secret_data.get("secret", "")
    except Exception as e:
        print(f"Error getting JWT secret: {e}")
        return None

def verify_jwt(token):
    """
    Verify JWT token signature and expiration.
    Returns: (payload_dict, None) on success or (None, error_message) on failure
    """
    try:
        secret = get_jwt_secret()
        if not secret:
            return None, "Auth service unavailable"
        
        # JWT format: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return None, "Invalid token format"
        
        header_b64, payload_b64, signature_b64 = parts

        # Validate the alg header — refuse anything other than HS256.
        # Defense against alg-confusion / "none"-alg forge attacks.
        try:
            header = json.loads(base64url_decode(header_b64))
        except Exception:
            return None, "Invalid token header"
        if header.get("alg") != "HS256":
            return None, "Invalid token algorithm"

        # Verify signature using HMAC-SHA256
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
        
        # Decode payload
        payload = json.loads(base64url_decode(payload_b64))
        
        # Check expiration
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
    # Check both lowercase and capitalized header names (API Gateway inconsistency)
    auth_header = headers.get('authorization', '') or headers.get('Authorization', '')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, "Authorization required"
    
    # Extract token after "Bearer " prefix
    token = auth_header[7:]
    return verify_jwt(token)

# ============================================
# DATABASE
# ============================================
def get_db_secret():
    """Retrieve database credentials from AWS Secrets Manager"""
    client = boto3.client("secretsmanager", region_name=REGION)
    return json.loads(client.get_secret_value(SecretId=os.getenv("DB_SECRET_ID", "bikeshop-credentials"))["SecretString"])

# ============================================
# TENANT CONFIG (multi-tenancy migration, step 7)
# Per-warm-container cache. tenant_id hardcoded to 1 (Brooklyn Bikery) at
# call sites until step 8 wires up URL-based tenant resolution.
# ============================================
_TENANT_CACHE: dict = {}

def get_tenant(tenant_id: int = 1) -> dict:
    """
    Load per-tenant config from the `tenants` table. Cached for the lifetime
    of the warm container — first call hits the DB, subsequent calls are free.
    Raises on missing tenant or non-active status (defensive: a shop we've
    suspended for non-payment shouldn't be able to process orders).
    """
    if tenant_id in _TENANT_CACHE:
        return _TENANT_CACHE[tenant_id]
    secret = get_db_secret()
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
                "status, invoice_footer FROM tenants WHERE id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Tenant {tenant_id} not found")
            cols = [d[0] for d in cur.description]
            tenant = dict(zip(cols, row))
            if tenant["status"] != "active":
                raise RuntimeError(
                    f"Tenant {tenant_id} ({tenant['slug']}) status is "
                    f"{tenant['status']!r}, refusing to serve"
                )
            _TENANT_CACHE[tenant_id] = tenant
            return tenant
    finally:
        conn.close()

def get_twilio_auth_token_for_tenant(tenant: dict) -> str:
    """Resolve the per-tenant Twilio auth token from Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=REGION)
    resp = client.get_secret_value(SecretId=tenant["twilio_auth_token_secret_arn"])
    data = json.loads(resp["SecretString"])
    return data.get("auth_token") or data.get("authToken") or ""

# ============================================
# HELPERS
# ============================================
def normalize_us_phone(phone_str: str) -> str | None:
    """
    Normalize phone number to E.164 format (+1XXXXXXXXXX).
    Accepts various formats: (555) 123-4567, 555-123-4567, 5551234567
    Returns: Normalized phone string or None if invalid
    """
    digits = "".join(ch for ch in (phone_str or "") if ch.isdigit())
    if not digits:
        return None
    
    # Handle 11-digit numbers starting with 1
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    
    # Handle 10-digit US numbers
    if len(digits) == 10:
        return f"+1{digits}"
    
    # Handle international numbers (already have + prefix)
    if phone_str.strip().startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    
    return None

# Fallback invoice footer for tenants that haven't set tenants.invoice_footer.
# Deliberately generic — a new shop must never inherit another shop's hours or
# payment methods. Brooklyn Bikery's real footer lives in its tenants row
# (set by migration 005), so BB invoices are unchanged.
DEFAULT_INVOICE_FOOTER = "Thank you! 🙏"


def _format_item_line(item):
    """Human line for one service line item on the invoice.

    item: dict with keys name, quantity, price, formula.
    Spoke-style quantity services render like the classic Brooklyn Bikery
    wording ("Front Fix 3 Spokes"); any other multi-quantity service falls
    back to "Name x3"; single fixed services are just the name.
    """
    name, qty = item["name"], int(item.get("quantity") or 1)
    if item.get("formula") == "spoke" and "Spoke" in name:
        plural = "s" if qty > 1 else ""
        return name.replace("Spoke", f"{qty} Spoke{plural}")
    if qty > 1:
        return f"{name} x{qty}"
    return name


def build_invoice_text(customer, order_id, date_str, line_items, subtotal, tax_rate, final_total, backend_notes, bike_description="", tenant_brand="BROOKLYN BIKERY", invoice_footer=None):
    """
    Build formatted SMS invoice text from catalog-driven line items.

    line_items: list of dicts {name, quantity, price, formula} — the same
    in-memory items that were priced and written to order_services, so the
    invoice always matches what was charged.

    tenant_brand comes from tenant['sms_sender_name']; invoice_footer comes
    from tenants.invoice_footer (per-shop hours / payment methods / sign-off).
    """
    lines = []
    lines.append(f"🚴 {tenant_brand}")
    lines.append("")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 {customer.get('name') or 'Customer'}")
    if bike_description:
        lines.append(f"🚲 {bike_description}")
    lines.append("")
    lines.append("🔧 SERVICES:")

    for item in line_items:
        lines.append(f"• {_format_item_line(item)}")
        lines.append(f"${float(item['price']):.2f}")

    # Add admin notes if any
    if backend_notes:
        lines.append("")
        lines.append("📝 NOTES:")
        lines.append(f"{backend_notes}")

    # Total line — all-caps TOTAL label for clear distinction, "+ Tax" tag indicates tax adds on top
    lines.append("")
    lines.append(f"💰 TOTAL: ${subtotal:.2f} + Tax")
    lines.append("")
    lines.append((invoice_footer or DEFAULT_INVOICE_FOOTER).strip())
    lines.append("")
    lines.append("Reply STOP to unsubscribe")
    return "\n".join(lines)

# ============================================
# MAIN HANDLER
# ============================================
def lambda_handler(event, context):
    """
    Main handler for backend service submission.
    
    GET /backend-submit: Returns phone number of most recent customer
    POST /backend-submit: Adds services to existing order and queues SMS invoice
    
    All requests require JWT authentication via Authorization header.
    """
    print(f"📥 Event received: {json.dumps(event)}")
    
    # Extract HTTP method (API Gateway format varies by version)
    http_method = (
        event.get('requestContext', {}).get('http', {}).get('method') or  # HTTP API v2
        event.get('requestContext', {}).get('httpMethod') or  # REST API
        event.get('httpMethod')  # Also REST API
    )
    print(f"📌 HTTP Method: {http_method}")

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
    
    # ============================================
    # ALL REQUESTS REQUIRE AUTHENTICATION
    # ============================================
    payload, error = require_auth(event)
    if error:
        print(f"❌ Auth failed: {error}")
        return response(401, {"error": error})
    
    print(f"✅ Authenticated request")

    # Multi-tenancy step 8: tenant comes from the VERIFIED token (absent -> 1,
    # so pre-step-8 tokens behave unchanged). Every customer/order/service query
    # below is scoped by this tid so a shop can never touch another's rows.
    tid = int(payload.get("tenant_id") or 1)

    # ============================================
    # GET request - Get latest phone number
    # Used by admin form to auto-populate phone field
    # ============================================
    if http_method == 'GET':
        try:
            secret = get_db_secret()
            conn = pymysql.connect(
                host=secret["host"],
                user=secret["user"],
                password=secret["password"],
                database=secret["database"],
                connect_timeout=5,
            )
            cursor = conn.cursor()
            
            # Get phone number of most recent order
            cursor.execute("""
                SELECT c.phone
                FROM orders o
                JOIN customers c ON o.customer_id = c.id AND c.tenant_id = o.tenant_id
                WHERE o.tenant_id = %s
                ORDER BY o.id DESC
                LIMIT 1
            """, (tid,))
            
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            phone = row[0] if row and row[0] else ""
            
            return response(200, {"phone": phone})
            
        except Exception as e:
            traceback.print_exc()
            return response(500, {"phone": "", "error": "Database error"})
    
    # ============================================
    # POST request - Submit services to existing order
    # ============================================
    try:
        # Resolve tenant config (multi-tenancy migration, step 7). tenant_id is
        # hardcoded to 1 (Brooklyn Bikery) until step 8 wires up URL routing.
        # Fail-fast: if tenant lookup breaks, the whole request fails — better
        # than silently using stale defaults that might charge wrong tax or
        # send SMS from the wrong shop's number.
        tenant = get_tenant(tid)

        body = json.loads(event.get("body", "{}"))

        # Extract form data
        name = (body.get("name") or "").strip()
        phone = (body.get("phone") or "").strip()
        lookup_phone = (body.get("lookupPhone") or "").strip()  # Phone to find customer
        is_new_customer = bool(body.get("isNewCustomer", False))
        bike_description = (body.get("bikeDescription") or "").strip()
        selected_services = body.get("services", []) or []
        front_spokes = int(body.get("frontSpokes", 0) or 0)
        rear_spokes = int(body.get("rearSpokes", 0) or 0)
        notes = (body.get("notes") or "").strip()
        custom_description = (body.get("customDescription") or "").strip()
        custom_price = float(body.get("customPrice", 0) or 0)
        # Whether to text the customer their invoice. Default True so the normal
        # "log a service -> invoice" flow is unchanged, and any older client that
        # doesn't send the flag keeps texting. Unchecked on the form -> save the
        # order silently (e.g. correcting a historical order without re-texting).
        send_invoice = bool(body.get("sendInvoice", True))

        # ── LEGACY label -> catalog code map ──────────────────────────────
        # The service-entry form used to send display labels like
        # "Front Flat ($25)". The current form sends catalog `serviceCodes`,
        # but an admin tab cached from before the switch may still send
        # labels. This map ONLY translates label -> code; the PRICE always
        # comes from the tenant's service_catalog row (single source of
        # truth), so a stale page can never charge a stale price.
        legacy_label_to_code = {
            "Front Flat ($25)": "front_flat",
            "Rear Flat ($25)": "rear_flat",
            "Front Flat E-Bike ($45)": "front_flat_ebike",
            "Rear Flat E-Bike ($45)": "rear_flat_ebike",
            "Front Brake Adj ($20)": "front_brake_adj",
            "Rear Brake Adj ($20)": "rear_brake_adj",
            "Front Brake Adj E-Bike ($35)": "front_brake_adj_ebike",
            "Rear Brake Adj E-Bike ($35)": "rear_brake_adj_ebike",
            "Front Replace V-Brake Pads ($10)": "front_replace_vbrake_pads",
            "Rear Replace V-Brake Pads ($10)": "rear_replace_vbrake_pads",
            "Front New V-Brake Pads ($15)": "front_new_vbrake_pads",
            "Rear New V-Brake Pads ($15)": "rear_new_vbrake_pads",
            "Front Replace Disc Brake Pads ($15)": "front_replace_disc_pads",
            "Rear Replace Disc Brake Pads ($15)": "rear_replace_disc_pads",
            "Front New Disc Brake Pads ($20)": "front_new_disc_pads",
            "Rear New Disc Brake Pads ($20)": "rear_new_disc_pads",
            "Front Hydraulic Brake Bleed ($50)": "front_hydraulic_brake_bleed",
            "Rear Hydraulic Brake Bleed ($50)": "rear_hydraulic_brake_bleed",
            "Front Derailleur Adj ($20)": "front_derailleur_adj",
            "Rear Derailleur Adj ($20)": "rear_derailleur_adj",
            "Tune-Up ($100)": "tune_up",
            "Replace Cassette/Freewheel ($15)": "replace_cassette",
            "New Bottom Bracket ($45)": "new_bb",
            "Replace Chain ($15)": "replace_chain",
            "Replace Crank/BB ($30)": "replace_crank_bb",
            "Replace Front Brake Line ($25)": "replace_front_brake_line",
            "Replace Rear Brake Line ($25)": "replace_rear_brake_line",
            "Replace Front Gear Line ($25)": "replace_front_gear_line",
            "Replace Rear Gear Line ($25)": "replace_rear_gear_line",
            "Front Wheel Truing ($20)": "front_wheel_true",
            "Rear Wheel Truing ($20)": "rear_wheel_true",
            "Replace Front Rotor ($15)": "replace_front_rotor",
            "Replace Rear Rotor ($15)": "replace_rear_rotor",
            "Front Repack Wheel ($25)": "front_repack_wheel",
            "Rear Repack Wheel ($25)": "rear_repack_wheel",
            "Repack Headset ($25)": "repack_headset",
            "Repack Headset E-Bike ($35)": "repack_headset_ebike",
            "E-Bike Diagnostic ($40)": "ebike_diagnostic",
            "Bike Assembly ($100)": "bike_assembly",
            "E-Bike Assembly ($150)": "ebike_assembly",
        }

        # Connect to database
        secret = get_db_secret()
        conn = pymysql.connect(
            host=secret["host"],
            user=secret["user"],
            password=secret["password"],
            database=secret["database"],
            connect_timeout=5,
        )
        cursor = conn.cursor()

        try:
            # ── getCatalog: the service-entry form fetches the tenant's menu ──
            # Returns the active service catalog so the form renders THIS
            # shop's services and prices instead of a hardcoded list. The
            # response is grouped client-side; we just send ordered rows.
            if body.get("action") == "getCatalog":
                cursor.execute(
                    "SELECT code, display_name, default_price, pricing_formula, "
                    "category, sort_order, additional_unit_price FROM service_catalog "
                    "WHERE tenant_id = %s AND is_active = 1 ORDER BY sort_order, id",
                    (tid,),
                )
                services = [
                    {
                        "code": r[0],
                        "name": r[1],
                        "price": float(r[2]) if r[2] is not None else None,
                        "formula": r[3],
                        "category": r[4] or "other",
                        "sort": r[5],
                        "add_price": float(r[6]) if r[6] is not None else None,
                    }
                    for r in cursor.fetchall()
                ]
                return response(200, {"services": services, "taxRate": float(tenant["tax_rate"])})

            today_str = str(datetime.now(ZoneInfo("America/New_York")).date())

            if is_new_customer:
                # ── New customer flow: create customer + fresh order ──────────
                new_phone = normalize_us_phone(phone)
                if not name or not new_phone:
                    return response(400, {"message": "Name and phone are required for new customers"})

                # Find or create customer by last 10 digits of phone
                # Handles mixed storage formats (10-digit vs E.164) across form types
                digits_10 = new_phone[-10:]
                cursor.execute("SELECT id FROM customers WHERE tenant_id = %s AND RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) = %s", (tid, digits_10))
                row = cursor.fetchone()
                if row:
                    customer_id = row[0]
                    # Existing customer — preserve their name, just create a new order
                else:
                    # Record SMS consent as captured at the counter (TCPA).
                    # Default True preserves the classic flow for older
                    # clients that don't send the field.
                    sms_consent = 1 if body.get("smsConsent", True) else 0
                    cursor.execute(
                        "INSERT INTO customers (tenant_id, name, phone, date_created, sms_consent, sms_consent_at) "
                        "VALUES (%s, %s, %s, %s, %s, NOW())",
                        (tid, name, new_phone, today_str, sms_consent)
                    )
                    customer_id = cursor.lastrowid

                # Create a new order for this visit
                cursor.execute(
                    "INSERT INTO orders (tenant_id, customer_id, date_of_service) VALUES (%s, %s, %s)",
                    (tid, customer_id, today_str)
                )
                order_id = cursor.lastrowid
                order_date = today_str

            else:
                # ── Existing customer / edit flow ─────────────────────────────
                #
                # WRONG-ORDER CORRUPTION FIX: prefer an explicit orderId. The
                # admin dashboard's "Edit Services" button now always sends the
                # exact order it opened, so we edit THAT order — never "the most
                # recent order for whatever phone is in the box". The old
                # phone -> most-recent-order heuristic meant a stale or mangled
                # lookupPhone could silently edit a *different customer's* order
                # (this corrupted two real customers' orders). orderId wins.
                order_id_param = body.get("orderId")

                if order_id_param not in (None, "", "null"):
                    try:
                        oid = int(order_id_param)
                    except (TypeError, ValueError):
                        return response(400, {"message": f"Invalid orderId: {order_id_param!r}"})

                    cursor.execute(
                        "SELECT id, date_of_service, customer_id FROM orders "
                        "WHERE id = %s AND tenant_id = %s",
                        (oid, tid),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return response(404, {"message": f"Order {oid} not found."})
                    order_id, order_date, customer_id = row[0], row[1], row[2]

                    # Defense-in-depth: if a lookupPhone was also supplied, it
                    # MUST match the order's real customer. A mismatch means the
                    # form's state is inconsistent (e.g. stale JS) — refuse the
                    # edit loudly rather than risk corrupting the wrong person.
                    if lookup_phone:
                        lookup_digits_10 = "".join(ch for ch in lookup_phone if ch.isdigit())[-10:]
                        cursor.execute(
                            "SELECT RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) "
                            "FROM customers WHERE id = %s AND tenant_id = %s",
                            (customer_id, tid),
                        )
                        cust_row = cursor.fetchone()
                        cust_digits_10 = cust_row[0] if cust_row else None
                        if lookup_digits_10 and cust_digits_10 and lookup_digits_10 != cust_digits_10:
                            return response(409, {
                                "message": "Safety check failed: the phone on the form does not match the "
                                           "customer on this order. Edit refused to prevent corrupting the "
                                           "wrong customer's order. Reload the dashboard and try again."
                            })
                else:
                    # ── Legacy fallback: no orderId. Manual phone entry to add
                    # services to a customer's latest order. Preserves the
                    # original Brooklyn Bikery workflow for that case.
                    lookup_digits_10 = "".join(ch for ch in (lookup_phone or "") if ch.isdigit())[-10:]
                    cursor.execute("SELECT id FROM customers WHERE tenant_id = %s AND RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) = %s", (tid, lookup_digits_10))
                    row = cursor.fetchone()
                    if not row:
                        return response(400, {"message": "Customer not found"})
                    customer_id = row[0]

                    # Get most recent order for this customer
                    cursor.execute("SELECT id, date_of_service FROM orders WHERE customer_id = %s AND tenant_id = %s ORDER BY id DESC LIMIT 1", (customer_id, tid))
                    row = cursor.fetchone()
                    if not row:
                        return response(400, {"message": "No existing order found for this customer."})
                    order_id, order_date = row[0], row[1]

                # Update customer info if provided (name/phone corrections)
                update_fields, vals = [], []
                if name:  update_fields.append("name = %s");  vals.append(name)
                if phone: update_fields.append("phone = %s"); vals.append(phone)
                if update_fields:
                    vals.append(customer_id)
                    vals.append(tid)
                    cursor.execute(f"UPDATE customers SET {', '.join(update_fields)} WHERE id = %s AND tenant_id = %s", vals)

            # Build the order record updates.
            # ────────────────────────────────────────────────────────────────
            # Step 6 of the multi-tenancy migration: service data has moved
            # entirely into `order_services` (line-items linked to
            # `service_catalog`). The `orders` row now only carries metadata
            # (notes, bike description) and the denormalized totals (price,
            # final_price). The legacy boolean service columns + spoke counts
            # + custom_service trio that lived on `orders` are no longer
            # written here and are being dropped by migration 004.
            # ────────────────────────────────────────────────────────────────
            order_updates = {"backend_notes": notes}
            if bike_description:
                order_updates["bike_description"] = bike_description

            # ── Catalog-driven pricing (single source of truth) ──────────────
            # Load the tenant's active catalog once; every price on this order
            # comes from it. The form sends `serviceCodes` (catalog codes); a
            # stale cached form may still send legacy `services` labels, which
            # are translated to codes — but even then the PRICE is the
            # catalog's, never the label's.
            cursor.execute(
                "SELECT id, code, display_name, default_price, pricing_formula, additional_unit_price "
                "FROM service_catalog WHERE tenant_id = %s AND is_active = 1",
                (tid,),
            )
            catalog = {
                r[1]: {"id": r[0], "code": r[1], "name": r[2],
                       "price": float(r[3]) if r[3] is not None else None,
                       "formula": r[4],
                       "add_price": float(r[5]) if r[5] is not None else None}
                for r in cursor.fetchall()
            }

            requested_codes = list(body.get("serviceCodes") or [])
            for label in selected_services:  # legacy label fallback
                code = legacy_label_to_code.get(label)
                if code and code not in requested_codes:
                    requested_codes.append(code)

            # Build the order's line items: [{catalog_id, code, name, quantity,
            # price, formula, notes}] — used for the total, order_services rows,
            # AND the SMS invoice, so all three always agree.
            line_items = []
            unknown_codes = []
            for code in requested_codes:
                svc = catalog.get(code)
                if not svc:
                    unknown_codes.append(code)
                    continue
                if svc["formula"] != "fixed" or svc["price"] is None:
                    # spoke/custom services arrive via their dedicated fields
                    continue
                line_items.append({
                    "catalog_id": svc["id"], "code": code, "name": svc["name"],
                    "quantity": 1, "price": svc["price"], "formula": "fixed",
                    "notes": None,
                })
            if unknown_codes:
                # A code the tenant doesn't offer (or was deactivated between
                # page load and submit). Refuse loudly — silently dropping a
                # line item would under-charge without anyone noticing.
                return response(400, {"message": f"Unknown service(s): {', '.join(sorted(unknown_codes)[:5])}. Reload the form and try again."})

            # Spoke repairs (quantity-based). Now data-driven per shop, not a
            # hardcoded 33+2*qty: the line total is
            #   first-spoke price + each-additional * (qty - 1)
            # read from the catalog row (default_price / additional_unit_price).
            # Falls back to Brooklyn Bikery's historical $35 first / $2 each if a
            # row predates migration 007, so behavior is unchanged for BB.
            for qty, code in ((front_spokes, "front_fix_spoke"), (rear_spokes, "rear_fix_spoke")):
                if qty > 0 and code in catalog:
                    svc = catalog[code]
                    first = svc["price"] if svc["price"] is not None else 35.0
                    each = svc["add_price"] if svc.get("add_price") is not None else 2.0
                    spoke_total = round(first + each * (qty - 1), 2)
                    line_items.append({
                        "catalog_id": svc["id"], "code": code, "name": svc["name"],
                        "quantity": qty, "price": spoke_total, "formula": "spoke",
                        "notes": None,
                    })

            # Custom service (ad-hoc price + description set by the admin)
            if custom_description and custom_price > 0 and "custom_service" in catalog:
                svc = catalog["custom_service"]
                line_items.append({
                    "catalog_id": svc["id"], "code": "custom_service", "name": custom_description,
                    "quantity": 1, "price": custom_price, "formula": "custom",
                    "notes": custom_description,
                })

            total_price = round(sum(float(it["price"]) for it in line_items), 2)

            # Final price with per-tenant tax rate (Brooklyn Bikery = 0.0875 NYC)
            tax_rate = float(tenant["tax_rate"])
            final_price = total_price * (1 + tax_rate)
            order_updates["price"] = total_price
            order_updates["final_price"] = final_price

            # Defense-in-depth: column names should only ever be alphanumeric + underscore.
            # Today they come from a server-controlled dict literal, but this prevents
            # SQLi regression if a future edit accidentally pulls a column name from user input.
            for _col in order_updates:
                if not _col or not all(c.isalnum() or c == '_' for c in _col):
                    print(f"SECURITY: rejecting non-alphanumeric column name: {_col!r}")
                    return response(400, {"message": "Invalid request"})

            # Update order record (tenant-scoped: can only touch this tenant's order)
            cursor.execute(
                f"UPDATE orders SET {', '.join([f'{c} = %s' for c in order_updates.keys()])} WHERE id = %s AND tenant_id = %s",
                list(order_updates.values()) + [order_id, tid]
            )

            # ────────────────────────────────────────────────────────────────
            # WRITE order_services line items (multi-tenancy migration, step 6).
            # As of step 6, order_services is the SOLE source of truth for
            # service data — the legacy boolean columns on `orders` are dropped
            # by migration 004. A failure here is still logged and swallowed so
            # the order record + SMS invoice still go through (the invoice is
            # built from in-memory request data, not from DB reads), but a
            # failed write means the admin dashboard will show this order with
            # no service line items until manually fixed. Watch the
            # ⚠️ order_services write failed log carefully.
            # tenant_id is hardcoded to 1 (Brooklyn Bikery) until step 8 wires
            # up tenant resolution from the request URL.
            # ────────────────────────────────────────────────────────────────
            try:
                # Idempotency: an admin may resubmit the same order. Clear
                # existing line items and re-insert based on current selection.
                cursor.execute(
                    "DELETE FROM order_services WHERE tenant_id = %s AND order_id = %s",
                    (tid, order_id)
                )

                # The exact same line_items that produced the total (and that
                # the invoice will render) become the order_services rows.
                line_rows = [
                    (tid, order_id, it["catalog_id"], it["quantity"], it["price"], it["notes"])
                    for it in line_items
                ]

                if line_rows:
                    cursor.executemany(
                        "INSERT INTO order_services "
                        "(tenant_id, order_id, service_catalog_id, quantity, price_charged, notes) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        line_rows
                    )
                print(f"✅ order_services: {len(line_rows)} row(s) written for order {order_id}")
            except Exception as os_write_err:
                # Log and continue — the SMS invoice still goes out because it's
                # built from in-memory request data, not a DB read. But the
                # admin dashboard will show this order with no service line
                # items. Investigate the log and manually insert the missing
                # rows if this happens.
                print(f"⚠️ order_services write failed for order {order_id}: {os_write_err}")

            # Get updated customer info for SMS (incl. STOP opt-out state)
            cursor.execute("SELECT name, phone, sms_opted_out FROM customers WHERE id = %s AND tenant_id = %s", (customer_id, tid))
            crow = cursor.fetchone()
            customer = {"name": crow[0], "phone": crow[1], "sms_opted_out": bool(crow[2])}

            # TCPA: a customer who texted STOP must not receive invoice texts,
            # even if the admin left the box checked. The order still saves.
            if send_invoice and customer["sms_opted_out"]:
                print(f"⛔ Customer {customer_id} has opted out of SMS — invoice text suppressed")
                send_invoice = False
                sms_suppressed_reason = "optout"
            else:
                sms_suppressed_reason = None

            cursor.execute("SELECT bike_description FROM orders WHERE id = %s AND tenant_id = %s", (order_id, tid))
            bike_row = cursor.fetchone()
            bike_desc = bike_row[0] if bike_row and bike_row[0] else ""

            # Build invoice text now while DB connection is open
            target_phone = normalize_us_phone(customer.get("phone") or "")
            invoice_text = None
            if target_phone and send_invoice:
                date_str = str(order_date or datetime.now(ZoneInfo("America/New_York")).date())
                invoice_text = build_invoice_text(
                    customer=customer,
                    order_id=order_id,
                    date_str=date_str,
                    line_items=line_items,
                    subtotal=total_price,
                    tax_rate=tax_rate,
                    final_total=final_price,
                    backend_notes=notes,
                    bike_description=bike_desc,
                    tenant_brand=tenant["sms_sender_name"].upper(),
                    invoice_footer=tenant.get("invoice_footer"),
                )

                # Store outbound invoice in messages table so it appears in
                # conversation history. from_number is the tenant's Twilio
                # sender — read straight from the tenants table column rather
                # than fetching a Secrets Manager value to look it up.
                try:
                    twilio_from = tenant["twilio_from_number"]
                    cursor.execute("""
                        INSERT INTO messages (tenant_id, phone, direction, body, status, from_number, to_number)
                        VALUES (%s, %s, 'outbound', %s, 'queued', %s, %s)
                    """, (tenant["id"], target_phone, invoice_text, twilio_from, target_phone))
                    # Row id rides along in the SMS job so Twilio's delivery
                    # status callback can update THIS row (sent/delivered/failed).
                    invoice_message_row_id = cursor.lastrowid
                except Exception as msg_err:
                    invoice_message_row_id = None
                    print(f"⚠️ Failed to store invoice in messages table: {msg_err}")

            conn.commit()

        finally:
            cursor.close()
            conn.close()

        if not send_invoice:
            # Either the admin chose not to text the customer (sms:"skipped"),
            # or the customer texted STOP and the send was suppressed for
            # compliance (sms:"optout" — the UI tells the admin why).
            # message must be exactly "Success" for the UI overlay.
            return response(200, {"message": "Success", "order_id": order_id,
                                  "sms": sms_suppressed_reason or "skipped"})

        if not target_phone:
            return response(200, {"message": "Success (no SMS sent: invalid/missing customer phone)", "order_id": order_id})

        # Upload SMS job to S3 for processing by separate SMS Lambda.
        #
        # The job payload carries the tenant's Twilio credentials directly so
        # SendSMS doesn't have to query the DB (SendSMS is NOT in a VPC and
        # therefore can't reach the RDS instance). Secrets Manager is
        # reachable from outside the VPC, so passing the secret ARN works.
        # account_sid + from_number are not secrets (just identifiers), so
        # they go in the payload as plain values.
        s3_client = boto3.client('s3')
        sms_bucket = os.getenv("SMS_BUCKET", "brooklyn-bikery-sms")
        job_key = f"sms/invoice_{order_id}_{uuid.uuid4().hex[:8]}.json"
        s3_client.put_object(
            Bucket=sms_bucket,
            Key=job_key,
            Body=json.dumps({
                "tenant_id": tenant["id"],
                "to": target_phone,
                "body": invoice_text,
                "twilio_account_sid": tenant["twilio_account_sid"],
                "twilio_auth_token_secret_arn": tenant["twilio_auth_token_secret_arn"],
                "twilio_from_number": tenant["twilio_from_number"],
                # Lets Twilio's status callback update this exact message row.
                "message_row_id": invoice_message_row_id,
            }),
            ContentType='application/json'
        )

        return response(200, {"message": "Success", "order_id": order_id, "sms": "queued"})

    except Exception as e:
        traceback.print_exc()
        return response(500, {"message": "Internal error"})