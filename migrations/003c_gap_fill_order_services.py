"""
Gap-fill `order_services` for any orders that were placed AFTER the original
backfill (003b) but BEFORE the dual-write Lambda code (step 4) deployed.

These orders have the legacy boolean columns set on `orders` but no
corresponding rows in `order_services`. Without this gap-fill, step 5's read
switch would render them as "no services performed" in the admin dashboard
while still showing their actual stored price — confusing and wrong.

Pricing rules are copied from 003b (which copied from Backend-Form.py:340-381).

Idempotent: only operates on orders that have ZERO existing order_services
rows. Existing rows are not touched, so this can be re-run safely.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

import boto3
import pymysql

# Re-imported from 003b for self-containment — keep these dicts in sync if
# pricing ever changes (also update Backend-Form.py:340-381 and migration 003
# seed).
FIXED_SERVICES: dict[str, Decimal] = {
    "front_flat":                 Decimal("25"),
    "rear_flat":                  Decimal("25"),
    "front_flat_ebike":           Decimal("45"),
    "rear_flat_ebike":            Decimal("45"),
    "front_brake_adj":            Decimal("20"),
    "rear_brake_adj":             Decimal("20"),
    "front_brake_adj_ebike":      Decimal("35"),
    "rear_brake_adj_ebike":       Decimal("35"),
    "front_replace_vbrake_pads":  Decimal("10"),
    "rear_replace_vbrake_pads":   Decimal("10"),
    "front_new_vbrake_pads":      Decimal("15"),
    "rear_new_vbrake_pads":       Decimal("15"),
    "front_replace_disc_pads":    Decimal("15"),
    "rear_replace_disc_pads":     Decimal("15"),
    "front_new_disc_pads":        Decimal("20"),
    "rear_new_disc_pads":         Decimal("20"),
    "front_hydraulic_brake_bleed":Decimal("50"),
    "rear_hydraulic_brake_bleed": Decimal("50"),
    "front_derailleur_adj":       Decimal("20"),
    "rear_derailleur_adj":        Decimal("20"),
    "tune_up":                    Decimal("100"),
    "replace_cassette":           Decimal("15"),
    "new_bb":                     Decimal("45"),
    "replace_chain":              Decimal("15"),
    "replace_crank_bb":           Decimal("30"),
    "replace_front_brake_line":   Decimal("25"),
    "replace_rear_brake_line":    Decimal("25"),
    "replace_front_gear_line":    Decimal("25"),
    "replace_rear_gear_line":     Decimal("25"),
    "front_wheel_true":           Decimal("20"),
    "rear_wheel_true":            Decimal("20"),
    "replace_front_rotor":        Decimal("15"),
    "replace_rear_rotor":         Decimal("15"),
    "front_repack_wheel":         Decimal("25"),
    "rear_repack_wheel":          Decimal("25"),
    "repack_headset":             Decimal("25"),
    "repack_headset_ebike":       Decimal("35"),
    "ebike_diagnostic":           Decimal("40"),
    "bike_assembly":              Decimal("100"),
    "ebike_assembly":             Decimal("150"),
}
SPOKE_COLUMNS = ("front_fix_spoke", "rear_fix_spoke")
SPOKE_BASE = Decimal("33")
SPOKE_PER_UNIT = Decimal("2")


def get_db_creds() -> dict:
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    return json.loads(sm.get_secret_value(SecretId="bikeshop-credentials")["SecretString"])


def find_gap_orders(cur) -> list[dict]:
    """Return orders that have zero order_services rows but a non-null price."""
    cols = (
        ", ".join(FIXED_SERVICES.keys())
        + ", "
        + ", ".join(SPOKE_COLUMNS)
        + ", custom_service, custom_description, custom_service_price, price"
    )
    cur.execute(
        f"""
        SELECT o.id, {cols}
        FROM orders o
        LEFT JOIN order_services os ON os.order_id = o.id
        WHERE o.tenant_id = 1 AND os.id IS NULL AND o.price IS NOT NULL AND o.price > 0
        GROUP BY o.id
        ORDER BY o.id
        """
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def compute_rows(order: dict, catalog: dict[str, int]) -> tuple[list[tuple], Decimal]:
    rows: list[tuple] = []
    subtotal = Decimal("0")

    for col, price in FIXED_SERVICES.items():
        if order.get(col):
            rows.append((1, order["id"], catalog[col], 1, str(price), None))
            subtotal += price

    for col in SPOKE_COLUMNS:
        qty = order.get(col) or 0
        if qty > 0:
            sp = SPOKE_BASE + SPOKE_PER_UNIT * Decimal(qty)
            rows.append((1, order["id"], catalog[col], qty, str(sp), None))
            subtotal += sp

    if order.get("custom_service") and (order.get("custom_service_price") or 0) > 0:
        cp = Decimal(order["custom_service_price"])
        rows.append((1, order["id"], catalog["custom_service"], 1, str(cp),
                     order.get("custom_description")))
        subtotal += cp

    return rows, subtotal


def main(argv: list[str]) -> int:
    commit_mode = "--commit" in argv
    creds = get_db_creds()
    conn = pymysql.connect(
        host=creds["host"], user=creds["user"], password=creds["password"],
        database=creds["database"], charset="utf8mb4", autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, id FROM service_catalog WHERE tenant_id = 1")
            catalog = {code: cid for code, cid in cur.fetchall()}

            gaps = find_gap_orders(cur)
            print(f"=== DRY RUN === Found {len(gaps)} order(s) needing gap-fill.")
            if not gaps:
                print("Nothing to do. Exiting clean.")
                return 0

            all_inserts: list[tuple] = []
            for order in gaps:
                rows, computed = compute_rows(order, catalog)
                stored = Decimal(order["price"])
                delta = (stored - computed).quantize(Decimal("0.01"))
                if delta > 0:
                    desc = order.get("custom_description")
                    note = desc if desc else "Backfill reconciliation: historical price"
                    rows.append((1, order["id"], catalog["custom_service"], 1,
                                 str(delta), note))
                    computed += delta
                print(
                    f"  order {order['id']:4}  price={stored:>8}  computed_sum={computed:>8}  "
                    f"rows={len(rows)}"
                )
                all_inserts.extend(rows)

            print(f"\nTotal rows to insert: {len(all_inserts)}")
            if not commit_mode:
                print("\nNo --commit flag. No writes performed.")
                return 0

            print("\n=== COMMIT ===")
            cur.executemany(
                "INSERT INTO order_services "
                "(tenant_id, order_id, service_catalog_id, quantity, price_charged, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                all_inserts,
            )
            conn.commit()
            print(f"Inserted {len(all_inserts)} rows.")
            return 0
    except Exception:
        conn.rollback()
        print("ROLLED BACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
