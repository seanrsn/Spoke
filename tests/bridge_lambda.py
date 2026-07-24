"""Staging DB bridge for the CI test suite (runs INSIDE the VPC).

GitHub-hosted runners cannot reach RDS — the DB security group is
IP-allowlisted and runner IPs are dynamic. This function shares the app
Lambdas' VPC/SG, so it can. The integration suite runs on the CI runner and
routes ONLY its SQL through here via lambda:InvokeFunction (IAM-gated).

Safety scoping:
  * Connects exclusively through bikeshop-credentials-staging and asserts the
    resolved database is bikeshop_staging.
  * Refuses any statement that schema-qualifies a NON-staging schema
    (prod `bikeshop.`, `information_schema.`, `mysql.`) or issues USE.
    `bikeshop_staging.` is allowed (it is the staging schema).
"""
import json
import re
import datetime
import decimal
import boto3
import pymysql

# Block cross-schema access. `bikeshop.` (prod) is blocked; `bikeshop_staging.`
# is NOT matched because "bikeshop" there is followed by "_", not whitespace/dot.
_FORBIDDEN = re.compile(
    r"(?is)"
    r"\bbikeshop\s*\.|"          # prod schema qualifier
    r"\binformation_schema\b|"
    r"\bmysql\s*\.|"
    r"\bperformance_schema\b|"
    r"\bsys\s*\.|"
    r"\buse\s+\w"                # USE <db>
)


def _jsonable(v):
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def lambda_handler(event, context):
    q = event.get("query")
    if not q:
        return {"ok": False, "error": "no query"}
    if _FORBIDDEN.search(q):
        return {"ok": False, "error": "statement references a non-staging schema"}
    params = event.get("params") or []

    sm = boto3.client("secretsmanager", region_name="us-east-1")
    c = json.loads(sm.get_secret_value(SecretId="bikeshop-credentials-staging")["SecretString"])
    if c.get("database") != "bikeshop_staging":
        return {"ok": False, "error": "staging secret does not point at bikeshop_staging"}

    conn = pymysql.connect(
        host=c["host"], user=c["user"], password=c["password"], database=c["database"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        autocommit=True, connect_timeout=8,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall() if cur.description else []
            rows = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
            return {"ok": True, "rows": rows, "rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
