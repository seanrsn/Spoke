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

# Deployment stage. In "staging" the function is hard-blocked from sending live
# SMS regardless of which Twilio creds are present (belt-and-suspenders alongside
# Twilio test credentials). Prod leaves this unset -> "prod" -> normal behavior.
STAGE = os.environ.get("STAGE", "prod")
# Twilio TEST Account SIDs are a fixed magic value; live SIDs differ. When the
# real staging test SID is wired in (see staging plan Phase 6), this allowlist
# lets only that test account through while STAGE=staging.
STAGING_ALLOWED_TWILIO_SIDS = set(
    s.strip() for s in os.environ.get("STAGING_ALLOWED_TWILIO_SIDS", "").split(",") if s.strip()
)

# Delivery-status callbacks. When set, every outbound message asks Twilio to
# POST status updates (sent/delivered/failed) to this URL — the AdminDashboard
# webhook, which updates the message row identified by ?msgRowId=. This Lambda
# is deliberately DB-less (outside the VPC), so the row id travels via the
# callback URL instead of a DB write here.
STATUS_CALLBACK_URL = os.environ.get("STATUS_CALLBACK_URL", "")

# ============================================
# AWS CLIENTS
# ============================================
s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")

# ============================================
# TWILIO CREDS RESOLUTION (multi-tenancy migration, step 7 — revised)
#
# SendSMS lives OUTSIDE the VPC and therefore can't talk to RDS. So instead
# of looking up tenant config in the DB, the S3 job payload carries the
# Twilio credentials directly (Backend-Form just resolved them when it
# created the job). The auth_token still lives in Secrets Manager —
# Secrets Manager is reachable from outside the VPC.
#
# Legacy fallback: any in-flight jobs from before this change lack the
# new fields. For those we fall back to the original "twilio-credentials"
# secret which has the same shape as before.
# ============================================
LEGACY_TWILIO_SECRET = "twilio-credentials"

def _resolve_twilio_creds(job: dict):
    """
    Return (account_sid, auth_token, from_number) for an S3 SMS job. Prefers
    the new in-payload fields; falls back to the legacy Brooklyn Bikery
    twilio-credentials secret for jobs queued before the step 7 refactor.
    """
    arn = job.get("twilio_auth_token_secret_arn") or LEGACY_TWILIO_SECRET
    resp = secrets.get_secret_value(SecretId=arn)
    data = json.loads(resp.get("SecretString", "{}"))
    auth_token = data.get("auth_token") or data.get("authToken") or ""

    account_sid = job.get("twilio_account_sid") or data.get("account_sid")
    from_number = job.get("twilio_from_number") or data.get("from_number")
    return account_sid, auth_token, from_number

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

def send_sms(to: str, body: str, media_url: str | None = None, *, job: dict | None = None) -> Dict[str, Any]:
    """
    Send SMS via Twilio API using credentials from the S3 job payload.

    Args:
        to: Recipient phone number (E.164 format: +1XXXXXXXXXX)
        body: Message text
        media_url: Optional MMS media URL
        job: The S3 job dict, used to resolve per-tenant Twilio credentials.
             If None (e.g. push-notification path), uses the legacy
             twilio-credentials secret.

    Returns:
        Twilio API response containing message SID and status
    """
    if not to or not body:
        raise ValueError("'to' and 'body' are required")

    account_sid, auth_token, from_num = _resolve_twilio_creds(job or {})

    # ── STAGING HARD GUARD ────────────────────────────────────────────────────
    # In staging, refuse to hand anything to the live Twilio API unless the
    # resolved account is on the explicit test-SID allowlist. This is independent
    # of (and in addition to) using Twilio test credentials: even a misconfigured
    # staging job carrying a LIVE account SID is blocked here, so a staging
    # message can never reach a real handset.
    if STAGE == "staging" and account_sid not in STAGING_ALLOWED_TWILIO_SIDS:
        print(f"BLOCKED: STAGE=staging refusing send for non-allowlisted Twilio "
              f"account (sid prefix {str(account_sid)[:8]}). to={to}")
        return {"ok": False, "blocked": True, "reason": "staging_guard", "to": to}

    # Must have either messaging service SID or from number
    if not (TWILIO_MESSAGING_SERVICE_SID or from_num):
        raise RuntimeError(
            "No twilio_from_number on job or in secret, and no "
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

    # Ask Twilio to report delivery status back to the admin webhook, tagged
    # with the messages-table row this send belongs to.
    row_id = (job or {}).get("message_row_id")
    if STATUS_CALLBACK_URL and row_id:
        fields["StatusCallback"] = f"{STATUS_CALLBACK_URL}?msgRowId={int(row_id)}"

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

            # Twilio creds come straight from the job payload (added by
            # Backend-Form when it knew the tenant). Legacy jobs missing the
            # fields fall back to the original twilio-credentials secret via
            # _resolve_twilio_creds — see send_sms() for details.
            tenant_id = msg.get("tenant_id", "(legacy)")

            msg_type = "MMS" if media_url else "SMS"
            print(f"📞 Sending {msg_type} to {to} for tenant_id={tenant_id}"
                  + (f" with media: {media_url}" if media_url else ""))
            resp = send_sms(to, text, media_url, job=msg)
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