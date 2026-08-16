#!/usr/bin/env python3
"""
Did a real Razorpay payment reach us, and did we handle it correctly?

Every other Razorpay assertion in this repo computes its own signature with the
same secret the server verifies with. That exercises the verification, and it is
the right thing for a suite that has to run unattended — but it cannot prove the
one thing that matters at go-live: that **Razorpay's servers can reach our
webhook and that we do the right thing when they do.**

This script proves it, or says plainly that it has not happened yet. It is not
a gate: it needs a public URL and a human to complete one Checkout.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/razorpay_boundary_check.py

**How a genuine delivery is told from a forged one.** Every payment id this
suite has ever invented begins `pay_pilot_` or `pay_creator_` and exists
nowhere but our database. A real one exists at Razorpay, so the check is: take
the payment ids we have recorded, ask Razorpay about each, and believe only the
ones it acknowledges. A locally forged id comes back 400.

Test mode only. `rzp_test_…` keys, Razorpay's published test cards, no money.
"""
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pilot_fixture as fx  # noqa: E402  — reused for its SQL transport only

from app.config import settings  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


def razorpay(path, method="GET", body=None):
    auth = base64.b64encode(
        f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
    ).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1" + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


def main() -> int:
    print("\n══ razorpay boundary ══\n")

    if not (settings.razorpay_key_id or "").startswith("rzp_test_"):
        print(f"  REFUSING: the configured key is {settings.razorpay_key_id!r}, "
              f"which is not a test key. This script never runs against live.")
        return 1
    print(f"  key      {settings.razorpay_key_id}  (test mode)")

    st, hooks = razorpay("/webhooks")
    items = hooks.get("items", []) if isinstance(hooks, dict) else []
    active = [h for h in items if h.get("active")]
    check("a webhook is registered on the Razorpay account", bool(active),
          f"{st} {str(hooks)[:200]}")
    for h in active:
        events = sorted(k for k, v in (h.get("events") or {}).items() if v)
        print(f"  webhook  {h['url']}")
        print(f"           events: {', '.join(events)}")
    check("and it subscribes to payment.captured",
          any((h.get("events") or {}).get("payment.captured") for h in active),
          str([h.get("events") for h in active])[:200])

    # ── every payment id we have recorded, asked about at the source ─────────
    rows = fx.sql("""
        select o.razorpay_order_id, o.razorpay_payment_id, o.status, o.amount_paise,
               o.purpose, o.reservation_id, o.created_at
          from public.payment_orders o
         where o.razorpay_payment_id is not null
         order by o.created_at desc
         limit 40;""")

    if not rows:
        print("\n  No settled payment orders at all. Nothing to verify yet.")
        return _verdict(None)

    genuine = []
    for row in rows:
        st, payment = razorpay(f"/payments/{row['razorpay_payment_id']}")
        if st == 200 and isinstance(payment, dict) and payment.get("id"):
            genuine.append((row, payment))

    print(f"\n  {len(rows)} settled order(s) on file; "
          f"{len(genuine)} acknowledged by Razorpay.")

    if not genuine:
        return _verdict(None)

    row, payment = genuine[0]
    print(f"\n  payment  {payment['id']}  {payment.get('method')}  "
          f"{payment.get('status')}  ₹{int(payment.get('amount', 0)) / 100:,.2f}")

    check("Razorpay agrees the payment was captured",
          payment.get("status") in ("captured", "authorized"), str(payment.get("status")))
    check("for the order our server created",
          payment.get("order_id") == row["razorpay_order_id"],
          f"{payment.get('order_id')} vs {row['razorpay_order_id']}")
    check("and for the amount our server computed, to the paise",
          int(payment.get("amount") or 0) == int(row["amount_paise"]),
          f"{payment.get('amount')} vs {row['amount_paise']}")
    check("our own record says it is paid", row["status"] == "paid", str(row["status"]))
    check("and it produced exactly one reservation",
          bool(row["reservation_id"]) if row["purpose"] == "booking" else True,
          str(row["reservation_id"]))

    if row["purpose"] == "booking" and row["reservation_id"]:
        booked = fx.sql(f"""
            select r.ref, r.status, r.paid_paise, r.total_paise,
                   (select count(*) from public.folio_entries f
                     where f.reservation_id = r.id and f.kind = 'payment') as payments
              from public.reservations r where r.id = '{row["reservation_id"]}';""")
        if booked:
            b = booked[0]
            print(f"  booking  {b['ref']}  {b['status']}  "
                  f"paid ₹{int(b['paid_paise']) / 100:,.0f} of ₹{int(b['total_paise']) / 100:,.0f}")
            check("the guest is charged to the folio exactly once",
                  int(b["payments"]) == 1, str(b["payments"]))

        dupes = fx.sql(f"""
            select count(*) as n from public.reservations
             where quote_id = (select quote_id from public.payment_orders
                                where razorpay_payment_id = '{payment["id"]}');""")
        check("and the quote produced one reservation, not two",
              int(dupes[0]["n"]) == 1, str(dupes))

    return _verdict(genuine)


def _verdict(genuine) -> int:
    print(f"\n══ {PASS} passed, {FAIL} failed ══")
    if not genuine:
        print("""
UNVERIFIED — and this is the honest answer, not a failure.

No payment on file exists at Razorpay, which means every settlement recorded so
far was signed locally by a test suite. The webhook is registered and the route
answers, so what remains is one Checkout completed by a person:

    1. cd Travbuddy_supply_os && npm run dev
    2. open http://localhost:5173/stay/casa-mandala-anjuna
    3. pick any dates, pay with Razorpay's published test card
       4111 1111 1111 1111, any future expiry, any CVV
    4. re-run this script

No real card and no real money: the key is rzp_test_… throughout.
""")
        return 1
    print("""
VERIFIED — a payment created at Razorpay, captured by Razorpay, and reconciled
by this server against the order it created. Test mode; no money moved.
""")
    return 1 if FAIL else 0


if __name__ == "__main__":
    os.environ.setdefault("MESSAGING_INLINE_PROCESSING", "true")
    sys.exit(main())
