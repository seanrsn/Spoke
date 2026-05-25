"""
Backfill `order_services` rows from existing `orders` boolean columns.

Two-phase by design:
  1. DRY RUN — compute every row that would be inserted, sum per order, compare
     to orders.price, and print a summary. NO writes.
  2. COMMIT — only runs when invoked with --commit and only after the dry-run
     report has been displayed.

Pricing rules are copied verbatim from Backend-Form.py:340-381 (the
`services_with_prices` dict + spoke formula + custom-service handling).

Pre-requisite: migration 003 has been run (service_catalog + order_services
tables exist, service_catalog is seeded for tenant 1).
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Iterable

import boto3
import pymysql

# ============================================================================
# Pricing config — keep in sync with Backend-Form.py:340-381
# ============================================================================

# {column_name_on_orders: price}
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

# Spokes use formula: price = 33 + 2 * quantity
SPOKE_COLUMNS = ("front_fix_spoke", "rear_fix_spoke")
SPOKE_BASE = Decimal("33")
SPOKE_PER_UNIT = Decimal("2")


def get_db_creds() -> dict:
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    return json.loads(sm.get_secret_value(SecretId="bikeshop-credentials")["SecretString"])


def load_catalog_map(cur) -> dict[str, int]:
    """Return {service_code: service_catalog_id} for tenant 1."""
    cur.execute(
        "SELECT code, id FROM service_catalog WHERE tenant_id = 1"
    )
    return {code: cid for code, cid in cur.fetchall()}


def compute_order_lines(order_row: dict, catalog: dict[str, int]) -> tuple[list[dict], Decimal]:
    """
    Compute the list of order_services rows that should exist for one order.
    Returns (rows, computed_subtotal).
    """
    rows: list[dict] = []
    subtotal = Decimal("0")

    # Fixed-price services
    for col, price in FIXED_SERVICES.items():
        if order_row.get(col):
            rows.append({
                "service_code": col,
                "service_catalog_id": catalog[col],
                "quantity": 1,
                "price_charged": price,
                "notes": None,
            })
            subtotal += price

    # Spokes — only insert if count > 0
    for col in SPOKE_COLUMNS:
        qty = order_row.get(col) or 0
        if qty > 0:
            price = SPOKE_BASE + SPOKE_PER_UNIT * Decimal(qty)
            rows.append({
                "service_code": col,
                "service_catalog_id": catalog[col],
                "quantity": qty,
                "price_charged": price,
                "notes": None,
            })
            subtotal += price

    # Custom service
    if order_row.get("custom_service") and (order_row.get("custom_service_price") or 0) > 0:
        custom_price = Decimal(order_row["custom_service_price"])
        rows.append({
            "service_code": "custom_service",
            "service_catalog_id": catalog["custom_service"],
            "quantity": 1,
            "price_charged": custom_price,
            "notes": order_row.get("custom_description"),
        })
        subtotal += custom_price

    return rows, subtotal


def fetch_orders(cur) -> list[dict]:
    """Pull every order with all relevant service columns."""
    service_cols = (
        ", ".join(FIXED_SERVICES.keys())
        + ", "
        + ", ".join(SPOKE_COLUMNS)
        + ", custom_service, custom_description, custom_service_price, price"
    )
    cur.execute(f"SELECT id, tenant_id, {service_cols} FROM orders ORDER BY id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def dry_run() -> tuple[list[tuple[int, list[dict]]], list[dict]]:
    """
    Compute backfill plan without writing. Returns:
      - plan: list of (order_id, rows_to_insert) — already includes reconciliation rows
      - reconciliations: list of dicts describing the reconciliation rows added.
    """
    creds = get_db_creds()
    conn = pymysql.connect(
        host=creds["host"], user=creds["user"], password=creds["password"],
        database=creds["database"], charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            catalog = load_catalog_map(cur)
            if len(catalog) < 43:
                raise RuntimeError(
                    f"service_catalog has only {len(catalog)} rows for tenant 1 — "
                    f"did migration 003 run? Expected 43."
                )

            # Safety check — refuse to run if order_services already has rows.
            cur.execute("SELECT COUNT(*) FROM order_services WHERE tenant_id = 1")
            existing = cur.fetchone()[0]
            if existing > 0:
                raise RuntimeError(
                    f"order_services already has {existing} rows for tenant 1. "
                    f"Backfill is single-shot — manual cleanup required if you want to re-run."
                )

            orders = fetch_orders(cur)
            print(f"Found {len(orders)} orders to backfill.\n")

            plan: list[tuple[int, list[dict]]] = []
            reconciliations: list[dict] = []

            for order in orders:
                rows, computed_sum = compute_order_lines(order, catalog)
                stored_price = Decimal(order.get("price") or 0)
                delta = (stored_price - computed_sum).quantize(Decimal("0.01"))

                # Reconciliation: when stored_price exceeds what we computed from
                # the boolean columns, add a single custom_service line item to
                # make SUM(price_charged) per order match orders.price by
                # construction. This captures two historical patterns:
                #   - Old orders pre-custom-feature where custom_description / price
                #     were never stored separately (price reflects the actual total).
                #   - Orders where custom_service was flagged but custom_service_price
                #     was left NULL (the custom amount is folded into orders.price).
                if delta > 0:
                    desc = order.get("custom_description")
                    note = desc if desc else "Backfill reconciliation: historical price"
                    rows.append({
                        "service_code": "custom_service",
                        "service_catalog_id": catalog["custom_service"],
                        "quantity": 1,
                        "price_charged": delta,
                        "notes": note,
                    })
                    reconciliations.append({
                        "order_id": order["id"],
                        "delta": delta,
                        "computed_sum": computed_sum,
                        "stored_price": stored_price,
                        "had_custom_description": bool(desc),
                        "note": note,
                    })
                elif delta < 0:
                    # Computed > stored: something is wrong (shouldn't happen with
                    # the current pricing logic). Print loudly but don't auto-fix.
                    print(
                        f"WARNING: order {order['id']}: computed {computed_sum} > stored {stored_price}. "
                        f"Skipping reconciliation; inspect manually."
                    )

                plan.append((order["id"], rows))

            return plan, reconciliations
    finally:
        conn.close()


def print_summary(plan: list[tuple[int, list[dict]]], reconciliations: list[dict]) -> None:
    total_rows = sum(len(rows) for _, rows in plan)
    recon_rows = len(reconciliations)
    print(f"Plan summary:")
    print(f"  Orders to process: {len(plan)}")
    print(f"  Total order_services rows to insert: {total_rows}")
    print(f"    of which standard service rows: {total_rows - recon_rows}")
    print(f"    of which reconciliation rows:   {recon_rows}")
    print(f"  Orders with NO line items: {sum(1 for _, rows in plan if not rows)}")
    if reconciliations:
        print(f"\nReconciliation rows (one per order with price gap):")
        for r in reconciliations:
            tag = "had custom desc " if r["had_custom_description"] else "no custom desc  "
            print(
                f"  order {r['order_id']:4}  {tag} delta=+{r['delta']:>7}  "
                f"computed={r['computed_sum']:>8}  stored={r['stored_price']:>8}  "
                f"note={r['note']!r}"
            )


def commit(plan: list[tuple[int, list[dict]]]) -> None:
    creds = get_db_creds()
    conn = pymysql.connect(
        host=creds["host"], user=creds["user"], password=creds["password"],
        database=creds["database"], charset="utf8mb4",
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            inserted = 0
            for order_id, rows in plan:
                for row in rows:
                    cur.execute(
                        "INSERT INTO order_services "
                        "(tenant_id, order_id, service_catalog_id, quantity, price_charged, notes) "
                        "VALUES (1, %s, %s, %s, %s, %s)",
                        (order_id, row["service_catalog_id"], row["quantity"],
                         str(row["price_charged"]), row["notes"]),
                    )
                    inserted += 1
        conn.commit()
        print(f"\nCommitted: {inserted} rows inserted into order_services.")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK due to error.")
        raise
    finally:
        conn.close()


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    commit_mode = "--commit" in args

    print("=== DRY RUN ===")
    plan, mismatches = dry_run()
    print_summary(plan, mismatches)

    if not commit_mode:
        print("\nNo --commit flag set. No writes performed.")
        print("Re-run with --commit to actually insert the rows.")
        return 0

    print("\n=== COMMIT ===")
    commit(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
