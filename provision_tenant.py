#!/usr/bin/env python3
"""
Provision a new tenant (shop) — multi-tenancy step 9.

Creates an admin-password secret, inserts a `tenants` row, and seeds that tenant's
`service_catalog` by copying tenant 1's catalog. Idempotent-ish: refuses to clobber
an existing slug.

Usage:
    python provision_tenant.py --slug acme --name "Acme Bikes" --db staging
    python provision_tenant.py --slug acme --name "Acme Bikes" --db prod \
        --allowed-origin https://acme.bluewrenchhq.com --password "s3cret"

Twilio is left unconfigured (a new shop wires up its own number later); the shop
can log in and manage orders immediately, but can't send SMS until Twilio is set.

Requires boto3, pymysql, and AWS credentials.
"""
import argparse, json, sys, secrets as pysecrets
import boto3, pymysql

REGION = "us-east-1"
DB_SECRET = {"prod": "bikeshop-credentials", "staging": "bikeshop-credentials-staging"}

def main():
    ap = argparse.ArgumentParser(description="Provision a new tenant/shop")
    ap.add_argument("--slug", required=True, help="URL-safe shop identifier, e.g. 'acme'")
    ap.add_argument("--name", required=True, help="Display name, e.g. 'Acme Bikes'")
    ap.add_argument("--db", choices=["prod", "staging"], default="prod")
    ap.add_argument("--password", help="Admin login password (random if omitted)")
    ap.add_argument("--allowed-origin", default="", help="CORS origin for this shop's frontend")
    ap.add_argument("--tax-rate", type=float, default=0.0875)
    ap.add_argument("--from-number", default="", help="Twilio sender number (optional)")
    ap.add_argument("--twilio-sid", default="", help="Twilio account SID (optional)")
    ap.add_argument("--twilio-secret-arn", default="", help="ARN of the Twilio auth-token secret (optional)")
    args = ap.parse_args()

    slug = args.slug.strip().lower()
    sm = boto3.client("secretsmanager", region_name=REGION)
    creds = json.loads(sm.get_secret_value(SecretId=DB_SECRET[args.db])["SecretString"])
    conn = pymysql.connect(host=creds["host"], user=creds["user"], password=creds["password"],
                           database=creds["database"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=10)
    print(f"Provisioning tenant '{slug}' into {creds['database']} ({args.db})")
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM tenants WHERE slug = %s", (slug,))
            if c.fetchone():
                print(f"ABORT: a tenant with slug '{slug}' already exists. Refusing to clobber.")
                sys.exit(1)

        # 1) admin password secret
        password = args.password or pysecrets.token_urlsafe(16)
        secret_name = f"bikery-admin-password-{slug}"
        try:
            arn = sm.create_secret(Name=secret_name, SecretString=json.dumps({"password": password}),
                                   Description=f"Admin password for tenant {slug}")["ARN"]
        except sm.exceptions.ResourceExistsException:
            sm.put_secret_value(SecretId=secret_name, SecretString=json.dumps({"password": password}))
            arn = sm.describe_secret(SecretId=secret_name)["ARN"]
        print(f"  admin-password secret: {secret_name}")

        # 2) tenants row (explicit id = max+1 so it works regardless of AUTO_INCREMENT)
        with conn.cursor() as c:
            c.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nid FROM tenants")
            new_id = c.fetchone()["nid"]
            c.execute(
                """INSERT INTO tenants
                   (id, slug, display_name, tax_rate, allowed_origin, twilio_account_sid,
                    twilio_auth_token_secret_arn, twilio_from_number, sms_sender_name,
                    admin_password_secret_arn, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')""",
                (new_id, slug, args.name, args.tax_rate, args.allowed_origin, args.twilio_sid,
                 args.twilio_secret_arn, args.from_number, args.name, arn))

            # 3) seed this tenant's service_catalog by copying tenant 1's
            c.execute("""
                INSERT INTO service_catalog
                    (tenant_id, code, display_name, default_price, pricing_formula, category, is_active, sort_order)
                SELECT %s, code, display_name, default_price, pricing_formula, category, is_active, sort_order
                FROM service_catalog WHERE tenant_id = 1
            """, (new_id,))
            seeded = c.rowcount

        conn.commit()
        print(f"\n*** Provisioned tenant id={new_id} slug='{slug}' ({seeded} services seeded) ***")
        print(f"  Login: send slug '{slug}' + password at login.")
        if not args.password:
            print(f"  Generated password: {password}")
        if not args.twilio_sid:
            print("  NOTE: Twilio not configured — this shop can manage orders but can't send SMS yet.")
    except Exception as e:
        conn.rollback()
        print(f"ROLLED BACK — {type(e).__name__}: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
