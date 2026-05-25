"""
Run a SQL migration file against the bikeshop RDS instance.

Usage:
    python run_migration.py <path-to-migration.sql>

Reads RDS credentials from Secrets Manager (`bikeshop-credentials`), parses the
SQL file into statements, and executes them one at a time inside a transaction.
On any error, the transaction rolls back and the script exits non-zero.

Pre-requisite: a recent RDS snapshot. The script does NOT take a snapshot —
that's a human-verified safety check before running.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import boto3
import pymysql


def get_db_creds() -> dict:
    """Pull MySQL connection info from Secrets Manager."""
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    resp = sm.get_secret_value(SecretId="bikeshop-credentials")
    return json.loads(resp["SecretString"])


def split_sql(sql_text: str) -> list[str]:
    """
    Split a SQL file into individual statements.

    Strips line comments (`-- ...`) before splitting on `;`. Block comments
    (`/* ... */`) and string-literal semicolons aren't handled — this migration
    set doesn't use either, but if a future migration does, switch to a real
    SQL parser (sqlparse).
    """
    lines = []
    for raw in sql_text.splitlines():
        # Drop full-line and inline `-- comment`s. SQL strings shouldn't contain
        # `--` outside comments in these migrations.
        stripped = re.sub(r"--.*$", "", raw).rstrip()
        if stripped:
            lines.append(stripped)
    joined = "\n".join(lines)
    statements = [s.strip() for s in joined.split(";")]
    return [s for s in statements if s]


def run_migration(sql_path: Path) -> None:
    creds = get_db_creds()
    print(f"Connecting to {creds['host']} as {creds['user']}...")
    conn = pymysql.connect(
        host=creds["host"],
        user=creds["user"],
        password=creds["password"],
        database=creds["database"],
        connect_timeout=10,
        charset="utf8mb4",
        autocommit=False,
    )
    print("Connected.")

    sql_text = sql_path.read_text(encoding="utf-8")
    statements = split_sql(sql_text)
    print(f"Parsed {len(statements)} statement(s) from {sql_path.name}.")

    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, start=1):
                preview = " ".join(stmt.split())[:120]
                print(f"\n[{i}/{len(statements)}] Executing: {preview}{'...' if len(preview) == 120 else ''}")
                cur.execute(stmt)
                if cur.rowcount >= 0:
                    print(f"    rows affected: {cur.rowcount}")
        conn.commit()
        print("\nMigration committed successfully.")
    except Exception as e:
        conn.rollback()
        print(f"\nERROR — transaction rolled back: {e}")
        raise
    finally:
        conn.close()


def verify_tenants() -> None:
    """Quick sanity check: select from the tenants table and print results."""
    creds = get_db_creds()
    conn = pymysql.connect(
        host=creds["host"],
        user=creds["user"],
        password=creds["password"],
        database=creds["database"],
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, display_name, tax_rate, allowed_origin, status "
                "FROM tenants"
            )
            rows = cur.fetchall()
            print("\nVerification — SELECT from tenants:")
            for row in rows:
                print(f"  {row}")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_migration.py <path-to-migration.sql>")
        sys.exit(1)
    sql_path = Path(sys.argv[1])
    if not sql_path.exists():
        print(f"File not found: {sql_path}")
        sys.exit(1)
    run_migration(sql_path)
    # If the migration touched the tenants table, do a quick verify.
    if "tenants" in sql_path.read_text(encoding="utf-8"):
        verify_tenants()
