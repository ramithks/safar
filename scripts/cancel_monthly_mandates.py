"""
One-off: cancel the old MONTHLY ₹499 autopay mandates.

Founding-partner pricing (2026-08-03) moved the plan from ₹499/month to
₹499/year. The partners who had already paid were granted plan time to
2027-08-01 by hand, so their existing monthly mandates must stop — otherwise
Razorpay keeps collecting ₹499 every month on top of a year they've been given.

Cancelled IMMEDIATELY, not at cycle end: the point is that no further invoice
is ever raised. Nothing is taken away by doing so — `stay_plan_expires_at` is
what gates the listings, and it already reads 2027-08-01. The partner's row is
updated to match so the Plan page stops claiming autopay is on, and a later
`subscription.cancelled` webhook lands on the same value.

Usage:
    python scripts/cancel_monthly_mandates.py          # dry run — shows the plan
    python scripts/cancel_monthly_mandates.py --run    # actually cancel
"""
import asyncio
import sys

sys.path.insert(0, ".")

# The mandates opened against the monthly plan. Kept explicit rather than
# queried: this is a one-off correction for known partners, and a query that
# drifts could cancel a mandate someone legitimately opened later.
MANDATES = [
    ("sub_TIZFLCjTYpIifB", "Rashmi Pradeep Shetty"),
    ("sub_TK8tfQN3mjBj6s", "Hotel Lotus"),
    # Erin Villa Coonoor (sub_TJheqwmoQvdcbQ) is already cancelled — left out
    # so a rerun doesn't log a pointless 400.
]


async def main(write: bool) -> None:
    from app.db.client import get_db
    from app.services import razorpay

    if not razorpay.configured():
        print("Razorpay is not configured — set the keys and rerun.")
        return

    db = get_db()
    for subscription_id, brand in MANDATES:
        current = await razorpay.fetch_subscription(subscription_id)
        status = (current or {}).get("status", "unknown")

        if status in ("cancelled", "completed", "expired"):
            print(f"skip  {brand:26} {subscription_id}  already {status}")
            continue

        if not write:
            print(f"would cancel  {brand:26} {subscription_id}  (currently {status})")
            continue

        result = await razorpay.cancel_subscription(subscription_id, at_cycle_end=False)
        if result is None:
            print(f"FAILED  {brand:26} {subscription_id}  — cancel it in the dashboard")
            continue

        db.table("partners").update(
            {"subscription_status": result.get("status") or "cancelled"}
        ).eq("subscription_id", subscription_id).execute()
        print(f"cancelled  {brand:26} {subscription_id}  → {result.get('status')}")

    if not write:
        print("\nDry run. Rerun with --run to cancel.")


if __name__ == "__main__":
    asyncio.run(main("--run" in sys.argv))
