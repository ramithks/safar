"""
What a suite owns, and giving it back.

Six real-database suites run against one Supabase project, and until Phase C
none of them said which rows were theirs. Two failures came out of that, both of
them reported as bugs in code that was working:

  * `pms_e2e` computed 261% occupancy, because five suites had each left a
    fortnight of stays on the property whose room count is the denominator.
  * `whatsapp_e2e` reported "a reply was stored: 0", because `frontdesk_e2e` had
    spent the shared wallet down to −459 and `usage_allowed()` had correctly
    paused the Guest agent.

Neither was a defect. Both were a suite failing on the consequences of another
suite's mutations, which is the specific thing that makes a test suite worthless
as a gate: a red run stops meaning "something broke".

So a suite now declares what it owns, before it does anything:

    FIXTURE = e2e_fixture.Fixture(
        label="sales",
        property_id=SOMEWHERE_ELSE,
        partner_id=OTHER_PARTNER,
        prefixes=("TB-SL",),
    )
    FIXTURE.open()

and that prints, in the output every run of every suite starts with:

    ── sales ─────────────────────────────────────────────
       property         Somewhere Else  dddddddd-…
       billing account  Somewhere Else  4a7bdd97-…
       wallet           2,988 credits available
       owns             reservations TB-SL·, quotes on this property

**Two ways to be isolated, and both are here.** The strongest is a property of
your own: `sales_e2e` runs on the second seeded property, which has its own
billing account and therefore its own wallet, so nothing it spends and nothing
it books can be seen by another suite at all. The weaker one, for the four
suites that must share Casa Mandala because that is where the seeded inventory
and the seeded staff are, is: sweep what you named, before *and* after, and
raise the wallet to a floor before you start.

**Before as well as after.** A run that dies halfway leaves its rows behind, and
the run that finds them should not inherit them. Sweeping only on exit means one
crash poisons the property until somebody notices — which is exactly how the
room count got to 72.

**Prefixes, never dates or counts.** A sweep that deletes "old" rows deletes
whatever a colleague was halfway through debugging. `TB-SL…` is unambiguously
this suite's and unambiguously nobody's booking.

**The wallet floor is a top-up, not a weakened gate.** Nothing here touches
`usage_allowed()`, the overdraft limit or the rate card. The suite buys credits
like a hotel would — `billing.grant()`, idempotent on the day so re-running
cannot mint them forever and hide a genuine runaway charge behind a fixture.
The closing line then reports what the run actually spent, so a suite whose
cost quietly doubles is visible rather than absorbed.
"""
import atexit
import logging

from app.db.client import get_db

import e2e_sweep

logger = logging.getLogger(__name__)

# What a suite tops its wallet up to before starting. Roughly four times the
# most expensive suite's spend, so a run never approaches the overdraft and a
# suite that suddenly costs four times as much still fails honestly rather than
# being propped up.
DEFAULT_CREDIT_FLOOR = 2000


def _short(value: str | None) -> str:
    return f"{value[:8]}…" if value else "—"


def _credits(value) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


class Fixture:
    """
    One suite's declared ownership of one property.

    `prefixes` are reservation reference prefixes. Quotes, holds, conversations
    and runs are not swept: they are not counted by anything, they carry the
    audit trail a failing run is diagnosed from, and deleting a quote would take
    the evidence of what a price was with it. Reservations are swept because
    they are the rows that change a hotel's arithmetic.
    """

    def __init__(
        self,
        *,
        label: str,
        property_id: str,
        partner_id: str,
        prefixes: tuple[str, ...],
        also_sweep: tuple[str, ...] = (),
        credit_floor: int = DEFAULT_CREDIT_FLOOR,
    ):
        self.label = label
        self.property_id = property_id
        self.partner_id = partner_id
        self.prefixes = prefixes
        # Other properties this suite writes stays to — the cross-property
        # negative fixtures. Swept by the same prefixes.
        self.also_sweep = also_sweep
        self.credit_floor = credit_floor

        self.account_id: str | None = None
        self.opening_credits: float | None = None
        self._closed = False

    # ── identity ─────────────────────────────────────────────────────────────

    @property
    def properties(self) -> list[str]:
        return [self.property_id, *self.also_sweep]

    def _describe(self) -> tuple[str, str]:
        db = get_db()
        prop = (
            db.table("properties").select("name")
            .eq("id", self.property_id).limit(1).execute().data or []
        )
        account = (
            db.table("billing_accounts").select("name")
            .eq("id", self.account_id).limit(1).execute().data or []
        ) if self.account_id else []
        return (
            prop[0]["name"] if prop else "unknown property",
            account[0]["name"] if account else "no billing account",
        )

    # ── the two calls a suite makes ──────────────────────────────────────────

    def open(self) -> None:
        """Announce, fund, and clear what a previous run left behind."""
        import asyncio

        from app.services import billing

        self.account_id = asyncio.run(billing.account_for_property(self.property_id))
        state = (
            asyncio.run(billing.wallet_state(self.account_id)) if self.account_id else {}
        )
        self.opening_credits = state.get("available")

        property_name, account_name = self._describe()
        print(f"\n── {self.label} " + "─" * max(4, 58 - len(self.label)))
        print(f"   property         {property_name}  {_short(self.property_id)}")
        print(f"   billing account  {account_name}  {_short(self.account_id)}")
        print(f"   wallet           {_credits(self.opening_credits)} credits available")
        print(f"   owns             reservations {', '.join(p + '…' for p in self.prefixes)}")

        self._top_up()
        swept = e2e_sweep.sweep_reservations(get_db(), self.properties, list(self.prefixes))
        if swept:
            print(f"   swept            {swept} stays left by a previous run")
        print()

        # Registered rather than left to the suite's own exit path, because the
        # runs worth cleaning up after are the ones that end on an exception.
        atexit.register(self.close)

    def close(self) -> None:
        """Give the rows back, and report what the run cost."""
        if self._closed:
            return
        self._closed = True

        import asyncio

        from app.services import billing

        try:
            swept = e2e_sweep.sweep_reservations(
                get_db(), self.properties, list(self.prefixes)
            )
        except Exception:  # noqa: BLE001 — a failed sweep must not mask the result
            logger.warning("could not sweep %s fixtures", self.label, exc_info=True)
            swept = 0

        spent = None
        closing = None
        if self.account_id:
            try:
                closing = asyncio.run(billing.wallet_state(self.account_id)).get("available")
                if closing is not None and self.opening_credits is not None:
                    spent = float(self.opening_credits) - float(closing)
            except Exception:  # noqa: BLE001
                # Reached from `atexit` after a suite died mid-run, by which
                # point the database executor has been shut down and every async
                # call raises. The sweep above uses the sync client and still
                # works, which is the half that matters — the spend line is
                # reporting, and losing it must not cost the cleanup.
                pass

        bits = [f"swept {swept} stays"] if swept else []
        if spent is not None:
            # Negative means the top-up outweighed the spend, which is the
            # normal case and is stated as the spend rather than as a gain.
            bits.append(f"{_credits(max(spent, 0))} credits spent")
        if closing is not None:
            bits.append(f"{_credits(closing)} left")
        print(f"\n── {self.label} closed: {', '.join(bits) if bits else 'nothing to clean up'}")

    # ── funding ──────────────────────────────────────────────────────────────

    def _top_up(self) -> None:
        """
        Raise the wallet to the floor, if it is below it.

        Idempotent on the day: `grant()` is keyed on the property and the date,
        so re-running the suite six times does not mint 30,000 credits. That
        matters — a fixture that keeps buying credits would hide exactly the
        runaway-charge bug this ledger exists to make visible.
        """
        import asyncio
        from datetime import date

        from app.services import billing

        if not self.account_id or self.opening_credits is None:
            return
        if float(self.opening_credits) >= self.credit_floor:
            return

        asyncio.run(billing.grant(
            billing_account_id=self.account_id,
            # 'adjustment', not 'topup': `credit_buckets_kind_check` does not
            # list the latter, and an adjustment is what this honestly is.
            kind="adjustment",
            credits=self.credit_floor * 2,
            idempotency_key=f"e2e-topup:{self.property_id}:{date.today().isoformat()}",
            source_type="test",
            actor_type="admin",
            metadata={"reason": f"{self.label}_e2e keeps the suites independent"},
        ))
        after = asyncio.run(billing.wallet_state(self.account_id)).get("available")
        print(f"   topped up        {_credits(self.opening_credits)} → {_credits(after)}")
        self.opening_credits = after
