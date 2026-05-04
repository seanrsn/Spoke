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
SECRETS_NAME = os.environ.get("TWILIO_SECRET_NAME", "twilio-credentials")
TWILIO_FROM = os.environ.get("TWILIO_FROM")  # Optional: override from number
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID")  # Optional: use messaging service
FAILED_PREFIX = os.environ.get("FAILED_PREFIX", "failed/")  # Folder for failed jobs (set to "" to disable)

# ============================================
# AWS CLIENTS
# ============================================
s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")

# ============================================
# TWILIO API HELPERS
# ============================================
def _get_twilio_creds():
    """
    Retrieve Twilio credentials from AWS Secrets Manager.
    
    Returns:
        Tuple of (account_sid, auth_token, from_number_or_None)
    """
    resp = secrets.get_secret_value(SecretId=SECRETS_NAME)
    data = json.loads(resp.get("SecretString", "{}"))
    return data["account_sid"], data["auth_token"], data.get("from_number")

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

def send_sms(to: str, body: str, media_url: str | None = None) -> Dict[str, Any]:
    """
    Send SMS via Twilio API.
    
    Args:
        to: Recipient phone number (E.164 format: +1XXXXXXXXXX)
        body: Message text
        media_url: Optional MMS media URL
    
    Returns:
        Twilio API response containing message SID and status
    """
    if not to or not body:
        raise ValueError("'to' and 'body' are required")

    # Get credentials from Secrets Manager
    account_sid, auth_token, secret_from = _get_twilio_creds()
    
    # Determine 'from' number: env var takes precedence, fallback to secret
    from_num = TWILIO_FROM or secret_from

    # Must have either messaging service SID or from number
    if not (TWILIO_MESSAGING_SERVICE_SID or from_num):
        raise RuntimeError("Set TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM or secret.from_number")

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
def handle_push_notifications(event):
    """
    Send web push notifications to all subscriptions.
    Called via async Lambda invoke from Admin-Dashboard.

    Event format:
    {
        "action": "send_push",
        "subscriptions": [{"endpoint": "...", "p256dh": "...", "auth": "..."}],
        "payload": "{...}",
        "vapid_private_key": "...",
        "vapid_claims": {"sub": "mailto:..."}
    }
    """
    if not PYWEBPUSH_AVAILABLE:
        print("⚠️ pywebpush not available, cannot send push notifications")
        return {"ok": False, "error": "pywebpush not available"}

    subscriptions = event.get("subscriptions", [])
    payload = event.get("payload", "")
    vapid_private_key = event.get("vapid_private_key", "")
    vapid_claims = event.get("vapid_claims", {})

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

            # Send SMS/MMS via Twilio
            msg_type = "MMS" if media_url else "SMS"
            print(f"📞 Sending {msg_type} to {to}" + (f" with media: {media_url}" if media_url else ""))
            resp = send_sms(to, text, media_url)
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