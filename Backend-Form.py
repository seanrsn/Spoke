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

# CORS headers for cross-origin requests from admin dashboard
CORS = {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
}

def response(status, body):
    """Helper to format API Gateway response with CORS headers"""
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}

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
        resp = client.get_secret_value(SecretId="bikery-jwt-secret")
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
    return json.loads(client.get_secret_value(SecretId="bikeshop-credentials")["SecretString"])

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

def build_invoice_text(customer, order_id, date_str, selected_services, front_spokes, rear_spokes, prices_map, subtotal, tax_rate, final_total, backend_notes, bike_description="", custom_description="", custom_price=0):
    """
    Build formatted SMS invoice text.
    Includes service list, pricing breakdown, tax calculation, and total.
    """
    lines = []
    lines.append("🚴 BROOKLYN BIKERY")
    lines.append("")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 {customer.get('name') or 'Customer'}")
    if bike_description:
        lines.append(f"🚲 {bike_description}")
    lines.append("")
    lines.append("🔧 SERVICES:")

    # Add selected services from the form
    for label in selected_services:
        tup = prices_map.get(label)
        if tup:
            _, price = tup
            # Remove price from label for cleaner display
            clean_label = label.split(' ($')[0] if ' ($' in label else label
            lines.append(f"• {clean_label}")
            lines.append(f"${price:.2f}")

    # Add front spoke repairs if any
    if front_spokes and front_spokes > 0:
        spoke_cost = 33 + 2 * front_spokes  # Base $35 + $2 per additional spoke
        lines.append(f"• Front Fix {front_spokes} Spoke{'s' if front_spokes > 1 else ''}")
        lines.append(f"${spoke_cost:.2f}")

    # Add rear spoke repairs if any
    if rear_spokes and rear_spokes > 0:
        spoke_cost = 33 + 2 * rear_spokes  # Base $35 + $2 per additional spoke
        lines.append(f"• Rear Fix {rear_spokes} Spoke{'s' if rear_spokes > 1 else ''}")
        lines.append(f"${spoke_cost:.2f}")

    # Add custom service if provided
    if custom_description and custom_price > 0:
        lines.append(f"• {custom_description}")
        lines.append(f"${custom_price:.2f}")

    # Add admin notes if any
    if backend_notes:
        lines.append("")
        lines.append("📝 NOTES:")
        lines.append(f"{backend_notes}")

    # Total line — all-caps TOTAL label for clear distinction, "+ Tax" tag indicates tax adds on top
    lines.append("")
    lines.append(f"💰 TOTAL: ${subtotal:.2f} + Tax")
    lines.append("")
    lines.append("⏰ Hours:")
    lines.append("Mon: Closed")
    lines.append("Tue: Closed")
    lines.append("Wed: 6:30-8:30 PM")
    lines.append("Thu-Fri: 10 AM-6 PM")
    lines.append("Sat-Sun: 10 AM-4 PM")
    lines.append("")
    lines.append("💳 Payment Methods:")
    lines.append("• Zelle, Venmo, CashApp, Cash")
    lines.append("• Credit cards (+2.9% fee)")
    lines.append("")
    lines.append("Thank you! 🙏")
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
    
    # Handle CORS preflight request
    if http_method == 'OPTIONS':
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
                "Access-Control-Allow-Headers": "Content-Type,Authorization",
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                "Access-Control-Max-Age": "86400"  # Cache preflight for 24 hours
            },
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
                JOIN customers c ON o.customer_id = c.id
                ORDER BY o.id DESC
                LIMIT 1
            """)
            
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

        # Service pricing map with front/rear component separation
        # Format: "Display Label": ("database_column", price)
        services_with_prices = {
            "Front Flat ($25)": ("front_flat", 25),
            "Rear Flat ($25)": ("rear_flat", 25),
            "Front Flat E-Bike ($45)": ("front_flat_ebike", 45),
            "Rear Flat E-Bike ($45)": ("rear_flat_ebike", 45),
            "Front Brake Adj ($20)": ("front_brake_adj", 20),
            "Rear Brake Adj ($20)": ("rear_brake_adj", 20),
            "Front Brake Adj E-Bike ($35)": ("front_brake_adj_ebike", 35),
            "Rear Brake Adj E-Bike ($35)": ("rear_brake_adj_ebike", 35),
            "Front Replace V-Brake Pads ($10)": ("front_replace_vbrake_pads", 10),
            "Rear Replace V-Brake Pads ($10)": ("rear_replace_vbrake_pads", 10),
            "Front New V-Brake Pads ($15)": ("front_new_vbrake_pads", 15),
            "Rear New V-Brake Pads ($15)": ("rear_new_vbrake_pads", 15),
            "Front Replace Disc Brake Pads ($15)": ("front_replace_disc_pads", 15),
            "Rear Replace Disc Brake Pads ($15)": ("rear_replace_disc_pads", 15),
            "Front New Disc Brake Pads ($20)": ("front_new_disc_pads", 20),
            "Rear New Disc Brake Pads ($20)": ("rear_new_disc_pads", 20),
            "Front Hydraulic Brake Bleed ($50)": ("front_hydraulic_brake_bleed", 50),
            "Rear Hydraulic Brake Bleed ($50)": ("rear_hydraulic_brake_bleed", 50),
            "Front Derailleur Adj ($20)": ("front_derailleur_adj", 20),
            "Rear Derailleur Adj ($20)": ("rear_derailleur_adj", 20),
            "Tune-Up ($100)": ("tune_up", 100),
            "Replace Cassette/Freewheel ($15)": ("replace_cassette", 15),
            "New Bottom Bracket ($45)": ("new_bb", 45),
            "Replace Chain ($15)": ("replace_chain", 15),
            "Replace Crank/BB ($30)": ("replace_crank_bb", 30),
            "Replace Front Brake Line ($25)": ("replace_front_brake_line", 25),
            "Replace Rear Brake Line ($25)": ("replace_rear_brake_line", 25),
            "Replace Front Gear Line ($25)": ("replace_front_gear_line", 25),
            "Replace Rear Gear Line ($25)": ("replace_rear_gear_line", 25),
            "Front Wheel Truing ($20)": ("front_wheel_true", 20),
            "Rear Wheel Truing ($20)": ("rear_wheel_true", 20),
            "Replace Front Rotor ($15)": ("replace_front_rotor", 15),
            "Replace Rear Rotor ($15)": ("replace_rear_rotor", 15),
            "Front Repack Wheel ($25)": ("front_repack_wheel", 25),
            "Rear Repack Wheel ($25)": ("rear_repack_wheel", 25),
            "Repack Headset ($25)": ("repack_headset", 25),
            "Repack Headset E-Bike ($35)": ("repack_headset_ebike", 35),
            "E-Bike Diagnostic ($40)": ("ebike_diagnostic", 40),
            "Bike Assembly ($100)": ("bike_assembly", 100),
            "E-Bike Assembly ($150)": ("ebike_assembly", 150),
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
            today_str = str(datetime.now(ZoneInfo("America/New_York")).date())

            if is_new_customer:
                # ── New customer flow: create customer + fresh order ──────────
                new_phone = normalize_us_phone(phone)
                if not name or not new_phone:
                    return response(400, {"message": "Name and phone are required for new customers"})

                # Find or create customer by last 10 digits of phone
                # Handles mixed storage formats (10-digit vs E.164) across form types
                digits_10 = new_phone[-10:]
                cursor.execute("SELECT id FROM customers WHERE RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) = %s", (digits_10,))
                row = cursor.fetchone()
                if row:
                    customer_id = row[0]
                    # Existing customer — preserve their name, just create a new order
                else:
                    cursor.execute(
                        "INSERT INTO customers (name, phone, date_created) VALUES (%s, %s, %s)",
                        (name, new_phone, today_str)
                    )
                    customer_id = cursor.lastrowid

                # Create a new order for this visit
                cursor.execute(
                    "INSERT INTO orders (customer_id, date_of_service) VALUES (%s, %s)",
                    (customer_id, today_str)
                )
                order_id = cursor.lastrowid
                order_date = today_str

            else:
                # ── Existing customer flow: lookup by phone ───────────────────
                # Use last 10 digits to handle mixed phone storage formats
                lookup_digits_10 = "".join(ch for ch in (lookup_phone or "") if ch.isdigit())[-10:]
                cursor.execute("SELECT id FROM customers WHERE RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), '-', ''), ' ', ''), '(', ''), ')', ''), 10) = %s", (lookup_digits_10,))
                row = cursor.fetchone()
                if not row:
                    return response(400, {"message": "Customer not found"})
                customer_id = row[0]

                # Get most recent order for this customer
                cursor.execute("SELECT id, date_of_service FROM orders WHERE customer_id = %s ORDER BY id DESC LIMIT 1", (customer_id,))
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
                    cursor.execute(f"UPDATE customers SET {', '.join(update_fields)} WHERE id = %s", vals)

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

            # Calculate total price from selected services
            total_price = 0
            for label in selected_services:
                _, price = services_with_prices.get(label, (None, None))
                if price is not None:
                    total_price += price

            # Spoke repairs ($33 base + $2 per spoke)
            if front_spokes > 0:
                total_price += 33 + 2 * front_spokes
            if rear_spokes > 0:
                total_price += 33 + 2 * rear_spokes

            # Custom service price
            if custom_price > 0:
                total_price += custom_price

            # Final price with NYC tax (8.75%)
            tax_rate = 0.0875
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

            # Update order record
            cursor.execute(
                f"UPDATE orders SET {', '.join([f'{c} = %s' for c in order_updates.keys()])} WHERE id = %s",
                list(order_updates.values()) + [order_id]
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
                TENANT_ID = 1

                cursor.execute(
                    "SELECT code, id FROM service_catalog WHERE tenant_id = %s",
                    (TENANT_ID,)
                )
                catalog_map = {code: cid for code, cid in cursor.fetchall()}

                # Idempotency: an admin may resubmit the same order. Clear
                # existing line items and re-insert based on current selection.
                cursor.execute(
                    "DELETE FROM order_services WHERE tenant_id = %s AND order_id = %s",
                    (TENANT_ID, order_id)
                )

                line_rows = []

                # Fixed-price services selected on the form
                for label in selected_services:
                    col, price = services_with_prices.get(label, (None, None))
                    if col and col in catalog_map and price is not None:
                        line_rows.append(
                            (TENANT_ID, order_id, catalog_map[col], 1, price, None)
                        )

                # Spoke services (quantity-based; price = 33 + 2 * count)
                if front_spokes > 0 and "front_fix_spoke" in catalog_map:
                    spoke_price = 33 + 2 * front_spokes
                    line_rows.append(
                        (TENANT_ID, order_id, catalog_map["front_fix_spoke"],
                         front_spokes, spoke_price, None)
                    )
                if rear_spokes > 0 and "rear_fix_spoke" in catalog_map:
                    spoke_price = 33 + 2 * rear_spokes
                    line_rows.append(
                        (TENANT_ID, order_id, catalog_map["rear_fix_spoke"],
                         rear_spokes, spoke_price, None)
                    )

                # Custom service (ad-hoc; price + description set by admin)
                if custom_description and custom_price > 0 and "custom_service" in catalog_map:
                    line_rows.append(
                        (TENANT_ID, order_id, catalog_map["custom_service"],
                         1, custom_price, custom_description)
                    )

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

            # Get updated customer info for SMS
            cursor.execute("SELECT name, phone FROM customers WHERE id = %s", (customer_id,))
            crow = cursor.fetchone()
            customer = {"name": crow[0], "phone": crow[1]}

            cursor.execute("SELECT bike_description FROM orders WHERE id = %s", (order_id,))
            bike_row = cursor.fetchone()
            bike_desc = bike_row[0] if bike_row and bike_row[0] else ""

            # Build invoice text now while DB connection is open
            target_phone = normalize_us_phone(customer.get("phone") or "")
            invoice_text = None
            if target_phone:
                date_str = str(order_date or datetime.now(ZoneInfo("America/New_York")).date())
                invoice_text = build_invoice_text(
                    customer=customer,
                    order_id=order_id,
                    date_str=date_str,
                    selected_services=selected_services,
                    front_spokes=front_spokes,
                    rear_spokes=rear_spokes,
                    prices_map=services_with_prices,
                    subtotal=total_price,
                    tax_rate=tax_rate,
                    final_total=final_price,
                    backend_notes=notes,
                    bike_description=bike_desc,
                    custom_description=custom_description,
                    custom_price=custom_price
                )

                # Store outbound invoice in messages table so it appears in conversation history
                try:
                    twilio_from = ""
                    try:
                        sm_client = boto3.client("secretsmanager", region_name=REGION)
                        twilio_data = json.loads(sm_client.get_secret_value(SecretId="twilio-credentials")["SecretString"])
                        twilio_from = twilio_data.get("from_number", "")
                    except Exception:
                        pass
                    cursor.execute("""
                        INSERT INTO messages (phone, direction, body, status, from_number, to_number)
                        VALUES (%s, 'outbound', %s, 'queued', %s, %s)
                    """, (target_phone, invoice_text, twilio_from, target_phone))
                except Exception as msg_err:
                    print(f"⚠️ Failed to store invoice in messages table: {msg_err}")

            conn.commit()

        finally:
            cursor.close()
            conn.close()

        if not target_phone:
            return response(200, {"message": "Success (no SMS sent: invalid/missing customer phone)", "order_id": order_id})

        # Upload SMS job to S3 for processing by separate SMS Lambda
        s3_client = boto3.client('s3')
        sms_bucket = 'brooklyn-bikery-sms'
        job_key = f"sms/invoice_{order_id}_{uuid.uuid4().hex[:8]}.json"
        s3_client.put_object(
            Bucket=sms_bucket,
            Key=job_key,
            Body=json.dumps({"to": target_phone, "body": invoice_text}),
            ContentType='application/json'
        )

        return response(200, {"message": "Success", "order_id": order_id, "sms": "queued"})

    except Exception as e:
        traceback.print_exc()
        return response(500, {"message": "Internal error"})