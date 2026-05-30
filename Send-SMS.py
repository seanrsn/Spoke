"""
SMS & Push Notification Processing Lambda
- S3-triggered: Processes SMS jobs uploaded to S3 bucket via Twilio API
- Async invoke: Sends web push notifications via pywebpush (called from Admin-Dashboard Lambda)
"""

import os
import json
import uuid
import base64
import urllib.parse
import urllib.request
import urllib.error
from typing import Dict, Any

import boto3

# Load pywebpush at module level (cached after cold start)
try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except Exception as e:
    print(f"⚠️ pywebpush not available: {e}")
    PYWEBPUSH_AVAILABLE = False

# ============================================
# ENVIRONMENT CONFIGURATION
# ============================================
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID")  # Optional: use messaging service
FAILED_PREFIX = os.environ.get("FAILED_PREFIX", "failed/")  # Folder for failed jobs (set to "" to disable)
REGION = os.environ.get("AWS_REGION", "us-east-1")

# ============================================
# AWS CLIENTS
# ============================================
s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")

# Lazy import — only opened when an SMS job needs DB tenant lookup. Keeps
# push-notification-only invocations from paying the import cost.
def _pymysql():
    import pymysql
    return pymysql

# ============================================
# TENANT CONFIG (multi-tenancy migration, step 7)
# ============================================
_TENANT_CACHE: dict = {}

def get_tenant(tenant_id: int = 1) -> dict:
    """
    Load per-tenant config from the `tenants` table. Cached for the lifetime
    of the warm container. SendSMS only needs this when actually sending SMS
    (S3 jobs carry tenant_id) — push notification invocations skip it.
    """
    if tenant_id in _TENANT_CACHE:
        return _TENANT_CACHE[tenant_id]
    db_resp = secrets.get_secret_value(SecretId="bikeshop-credentials")
    db_creds = json.loads(db_resp["SecretString"])
    pymysql = _pymysql()
    conn = pymysql.connect(
        host=db_creds["host"],
        user=db_creds["user"],
        password=db_creds["password"],
        database=db_creds["database"],
        connect_timeout=5,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, twilio_account_sid, twilio_auth_token_secret_arn, "
                "twilio_from_number, status FROM tenants WHERE id = %s",
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

# ============================================
# TWILIO API HELPERS
# ============================================
def _get_twilio_creds(tenant: dict):
    """
    Resolve Twilio credentials for the given tenant. account_sid and from_number
    are direct columns on the tenants table; auth_token lives in the secret
    referenced by twilio_auth_token_secret_arn.

    Returns:
        Tuple of (account_sid, auth_token, from_number_or_None)
    """
    resp = secrets.get_secret_value(SecretId=tenant["twilio_auth_token_secret_arn"])
    data = json.loads(resp.get("SecretString", "{}"))
    auth_token = data.get("auth_token") or data.get("authToken") or ""
    return tenant["twilio_account_sid"], auth_token, tenant.get("twilio_from_number")

def _twilio_post(path: str, account_sid: str, auth_token: str, form: Dict[str, str]) -> Dict[str, Any]:
    """
    Make authenticated POST request to Twilio API using urllib (no external dependencies).
    
    Args:
        path: API endpoint path (e.g., /2010-04-01/Accounts/{sid}/Messages.json)
        account_sid: Twilio account SID
        auth_token: Twilio auth token
        form: Form data to send (URL-encoded)
    
    Returns:
        JSON response from Twilio API
    """
    url = f"https://api.twilio.com{path}"
    
    # Encode form data as URL-encoded string
    payload = urllib.parse.urlencode(form).encode("utf-8")
    
    # Create Basic Auth header
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode("ascii")
    
    # Build and send request
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def send_sms(to: str, body: str, media_url: str | None = None, tenant: dict | None = None) -> Dict[str, Any]:
    """
    Send SMS via Twilio API using the given tenant's Twilio account.

    Args:
        to: Recipient phone number (E.164 format: +1XXXXXXXXXX)
        body: Message text
        media_url: Optional MMS media URL
        tenant: tenant config dict (from get_tenant). If None, defaults to
                tenant_id=1 (Brooklyn Bikery) — kept for backward compat with
                any callers not yet wired through step 7.

    Returns:
        Twilio API response containing message SID and status
    """
    if not to or not body:
        raise ValueError("'to' and 'body' are required")

    if tenant is None:
        tenant = get_tenant(1)

    # Per-tenant Twilio creds: account_sid + from_number come from the tenants
    # table directly; auth_token is in the secret referenced by the tenant's
    # twilio_auth_token_secret_arn.
    account_sid, auth_token, from_num = _get_twilio_creds(tenant)

    # Must have either messaging service SID or from number
    if not (TWILIO_MESSAGING_SERVICE_SID or from_num):
        raise RuntimeError(
            f"Tenant {tenant.get('slug')!r} has no twilio_from_number and no "
            "TWILIO_MESSAGING_SERVICE_SID env var set"
        )

    # Build request fields
    fields = {"To": to, "Body": body}
    
    # Use messaging service if available, otherwise use from number
    if TWILIO_MESSAGING_SERVICE_SID:
        fields["MessagingServiceSid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        fields["From"] = from_num
    
    # Add media URL for MMS (if provided)
    if media_url:
        fields["MediaUrl"] = media_url

    # Send via Twilio API
    return _twilio_post(f"/2010-04-01/Accounts/{account_sid}/Messages.json",
                        account_sid, auth_token, fields)

# ============================================
# S3 ERROR HANDLING
# ============================================
def _move_to_failed(bucket: str, key: str, reason: str):
    """
    Move failed SMS job to failed/ folder for debugging.
    Preserves original file with unique identifier and failure reason in metadata.
    
    Args:
        bucket: S3 bucket name
        key: Original object key
        reason: Failure reason to store in metadata
    """
    # Extract filename from path
    base = key.split("/")[-1]  # e.g., "invoice_123_abc.json"
    
    # Create new key in failed/ folder with unique ID
    if FAILED_PREFIX is not None:
        dst_key = f"{FAILED_PREFIX}{base.rsplit('.', 1)[0]}__{uuid.uuid4().hex}.json"
    else:
        dst_key = base
    
    # Copy object with failure metadata
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": key},
        Key=dst_key,
        MetadataDirective="REPLACE",
        Metadata={"failed_reason": reason[:200]}  # Truncate to 200 chars
    )
    
    # Delete original file
    s3.delete_object(Bucket=bucket, Key=key)

# ============================================
# PUSH NOTIFICATION HANDLER
# ============================================
_VAPID_CACHE = None  # module-level cache; survives across warm invocations

def _get_vapid_private_key():
    """Fetch VAPID private key from Secrets Manager (cached per Lambda warm cycle).

    Important: only cache on success. Caching an empty string (e.g., from a
    misconfigured secret) would persist for the lifetime of the warm container
    and silently break push notifications even after the secret is fixed.
    """
    global _VAPID_CACHE
    if _VAPID_CACHE:
        return _VAPID_CACHE
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    resp = sm.get_secret_value(SecretId="bikery-vapid-keys")
    keys = json.loads(resp["SecretString"])
    key = keys.get("privateKey") or keys.get("private_key") or ""
    if key:
        _VAPID_CACHE = key
    return key


def handle_push_notifications(event):
    """
    Send web push notifications to all subscriptions.
    Called via async Lambda invoke from Admin-Dashboard.

    Event format:
    {
        "action": "send_push",
        "subscriptions": [{"endpoint": "...", "p256dh": "...", "auth": "..."}],
        "payload": "{...}",
        "vapid_claims": {"sub": "mailto:..."}
    }

    NOTE: vapid_private_key is NOT in the event — fetched from Secrets Manager
    so it never sits in the (public-read) S3 push job.
    """
    if not PYWEBPUSH_AVAILABLE:
        print("⚠️ pywebpush not available, cannot send push notifications")
        return {"ok": False, "error": "pywebpush not available"}

    subscriptions = event.get("subscriptions", [])
    payload = event.get("payload", "")
    vapid_claims = event.get("vapid_claims", {})
    try:
        vapid_private_key = _get_vapid_private_key()
    except Exception as e:
        print(f"❌ Failed to fetch VAPID private key from Secrets Manager: {e}")
        return {"ok": False, "error": "vapid_unavailable"}
    if not vapid_private_key:
        print("❌ VAPID private key empty/missing in Secrets Manager")
        return {"ok": False, "error": "vapid_missing"}

    print(f"🔔 Sending push to {len(subscriptions)} subscription(s)")

    sent_count = 0
    for sub in subscriptions:
        endpoint = sub["endpoint"]
        subscription_info = {
            "endpoint": endpoint,
            "keys": {
                "p256dh": sub["p256dh"],
                "auth": sub["auth"]
            }
        }
        # FCM requires aud = origin of the push endpoint (e.g. https://fcm.googleapis.com)
        # Apple is lenient, but FCM is strict — derive aud per-endpoint
        parsed = urllib.parse.urlparse(endpoint)
        aud = f"{parsed.scheme}://{parsed.netloc}"
        claims = {**vapid_claims, "aud": aud}

        try:
            print(f"📨 Pushing to {endpoint[:60]}... (aud={aud})")
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=claims,
                timeout=10,
                ttl=86400
            )
            sent_count += 1
            print(f"✅ Push sent successfully")
        except WebPushException as e:
            print(f"❌ Push failed: {e}")
            # If subscription is gone (410), it's stale — log it (could auto-delete from DB here)
            if "410" in str(e) or "404" in str(e):
                print(f"⚠️ Stale subscription detected for {endpoint[:60]}")
        except Exception as e:
            print(f"❌ Push error: {e}")

    print(f"🔔 Push complete: {sent_count}/{len(subscriptions)} sent")
    return {"ok": True, "sent": sent_count, "total": len(subscriptions)}


# ============================================
# LAMBDA HANDLER
# ============================================
def lambda_handler(event, context):
    """
    Multi-purpose handler:
    1. S3-triggered: Process SMS jobs and send via Twilio
    2. Async invoke: Send web push notifications (action=send_push)
    """
    print("📝 Event received:", json.dumps(event))

    # Process each S3 event record
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"].strip()
        key = rec["s3"]["object"]["key"]

        # Route: push notification job (from Admin-Dashboard via S3)
        if key.startswith("push/"):
            print(f"🔔 Processing push job: {key}")
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                push_event = json.loads(obj["Body"].read())
                result = handle_push_notifications(push_event)
                s3.delete_object(Bucket=bucket, Key=key)
                print(f"✅ Push job completed and deleted: {key}")
            except Exception as e:
                print(f"❌ Push job failed: {e}")
            continue

        # Only process files in sms/ folder (prevents accidental triggers)
        if not key.startswith("sms/"):
            print(f"⏭️ Skipping non-sms key: {key}")
            continue

        try:
            print(f"📥 Processing: {key}")
            
            # Read SMS job from S3
            obj = s3.get_object(Bucket=bucket, Key=key)
            msg = json.loads(obj["Body"].read())

            # Extract message details
            to = msg["to"]
            text = msg["body"]
            # Support both camelCase (from admin dashboard) and snake_case (legacy)
            media_url = msg.get("mediaUrl") or msg.get("media_url")  # Optional MMS attachment

            # Resolve tenant — added to job payload in step 7. In-flight legacy
            # jobs from before step 7 lacked tenant_id; default to 1 (Brooklyn
            # Bikery) so we drain the queue without dropping invoices.
            tenant_id = int(msg.get("tenant_id") or 1)
            tenant = get_tenant(tenant_id)

            # Send SMS/MMS via Twilio (using this tenant's account)
            msg_type = "MMS" if media_url else "SMS"
            print(f"📞 Sending {msg_type} to {to} via tenant {tenant['slug']!r}"
                  + (f" with media: {media_url}" if media_url else ""))
            resp = send_sms(to, text, media_url, tenant=tenant)
            print({"ok": True, "type": msg_type, "twilio_sid": resp.get("sid"), "status": resp.get("status"), "to": to})

            # Delete job file after successful send
            s3.delete_object(Bucket=bucket, Key=key)
            print(f"✅ Deleted successfully processed file: {key}")

            # Delete MMS image from S3 after Twilio receives it (privacy/cleanup)
            if media_url and "mms-images/" in media_url:
                try:
                    # Extract bucket and key from the media URL
                    # URL format: https://bucket.s3.amazonaws.com/mms-images/filename.jpg
                    # or: https://s3.amazonaws.com/bucket/mms-images/filename.jpg
                    from urllib.parse import urlparse
                    parsed = urlparse(media_url)

                    # Handle both S3 URL formats
                    if parsed.netloc.endswith('.s3.amazonaws.com'):
                        # Format: bucket.s3.amazonaws.com/key
                        mms_bucket = parsed.netloc.replace('.s3.amazonaws.com', '')
                        mms_key = parsed.path.lstrip('/')
                    elif 's3.amazonaws.com' in parsed.netloc or 's3.us-east-1.amazonaws.com' in parsed.netloc:
                        # Format: s3.amazonaws.com/bucket/key
                        parts = parsed.path.lstrip('/').split('/', 1)
                        mms_bucket = parts[0]
                        mms_key = parts[1] if len(parts) > 1 else ''
                    else:
                        # Try to extract from path
                        mms_bucket = bucket  # Use same bucket as SMS queue
                        mms_key = parsed.path.lstrip('/')

                    if mms_key:
                        s3.delete_object(Bucket=mms_bucket, Key=mms_key)
                        print(f"🗑️ Deleted MMS image: {mms_bucket}/{mms_key}")
                except Exception as img_err:
                    # Don't fail the whole operation if image deletion fails
                    print(f"⚠️ Failed to delete MMS image: {img_err}")
            
        except Exception as e:
            # Log error and move file to failed/ folder for debugging
            print({"ok": False, "error": str(e), "key": key})
            try:
                _move_to_failed(bucket, key, str(e))
                print(f"⚠️ Moved failed file to: failed/{key}")
            except Exception as e2:
                # If moving to failed/ also fails, log but don't crash
                print({"failed_to_move": str(e2), "key": key})

    return {"ok": True}