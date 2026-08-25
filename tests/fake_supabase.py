"""
An in-memory stand-in for the Supabase client.

Enough of PostgREST's builder to run the agent runtime against: select with
equality filters, insert, update, upsert, and rpc. Not a database — it does not
enforce a check constraint or an RLS policy, and it is not asked to. Those are
proven by `supabase/tests/run.sh` against real Postgres and by
`scripts/pms_e2e.py` against the real project.

What this exists for is the middle layer: asserting that one guest sentence
produces one run row, three action rows in the right order with the right
statuses, and a task whose `agent_action_id` points back at the action that
raised it. Those are questions about the runtime's control flow, and answering
them against a real database would make the tests slow, ordered and flaky
without making them truer.
"""
import copy
import itertools
import uuid
from dataclasses import dataclass, field


@dataclass
class _Result:
    data: list
    count: int | None = None


class _Query:
    def __init__(self, store: dict, table: str, unique: dict | None = None):
        self._store = store
        self._table = table
        self._unique = unique or {}
        self._filters: list = []
        self._op = "select"
        self._payload = None
        self._order: tuple[str, bool, bool | None] | None = None
        self._limit: int | None = None
        self._conflict: str | None = None
        self._ignore_duplicates = False

    # ── builder ──────────────────────────────────────────────────────────────

    def select(self, *_cols, **_kw):
        if self._op == "select":
            self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False, **_kw):
        self._op, self._payload = "upsert", payload
        self._conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c)) == str(v))
        return self

    def or_(self, expression: str):
        """
        PostgREST's `or=(a.eq.1,b.eq.2)`, as the fake understands it.

        **Added because its absence was quietly narrowing coverage.** Every call
        to `negotiation._read_rules` raised `AttributeError: '_Query' object has
        no attribute 'or_'` — and `rules_for` catches that and falls back to the
        property's default rules, deliberately, because a failed read must never
        widen what may be given away. So nothing broke and nothing failed: the
        per-room-type and per-rate-plan negotiation rules simply had no test in
        the suite that reached them, on any path, ever.

        A fixture that turns a whole code path into a silent no-op is worse than
        one that raises. Only the `col.eq.value` form is parsed, which is the
        only form this codebase builds; anything else raises rather than
        matching everything, because a filter the fake ignores is how a test
        comes to pass against rows it should never have seen.
        """
        checks = []
        for clause in expression.split(","):
            column, _, rest = clause.partition(".")
            operator, _, value = rest.partition(".")
            if operator != "eq":
                raise NotImplementedError(
                    f"fake_supabase.or_ understands `col.eq.value`, not {clause!r}"
                )
            checks.append((column, value))
        self._filters.append(
            lambda r, cs=tuple(checks): any(str(r.get(c)) == str(v) for c, v in cs)
        )
        return self

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c)) != str(v))
        return self

    def in_(self, col, vals):
        wanted = {str(v) for v in vals}
        self._filters.append(lambda r, c=col: str(r.get(c)) in wanted)
        return self

    def is_(self, col, val):
        """
        PostgREST's null test. `is_("x", "null")` and nothing else — the real
        client also takes true/false, and neither this codebase nor the fake
        has a use for them yet.
        """
        if str(val).lower() != "null":
            raise NotImplementedError(f"fake is_ only handles null, not {val!r}")
        self._filters.append(lambda r, c=col: r.get(c) is None)
        return self

    def ilike(self, col, val):
        needle = str(val).strip("%").lower()
        self._filters.append(lambda r, c=col: needle in str(r.get(c) or "").lower())
        return self

    def gt(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c) or "") > str(v))
        return self

    def gte(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c) or "") >= str(v))
        return self

    def lte(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c) or "") <= str(v))
        return self

    def order(self, col, desc=False, nullsfirst=None, foreign_table=None):
        # `nullsfirst` is accepted because real callers pass it: Postgres reads
        # `order by x desc` as `desc nulls first`, which once filled an inbox
        # with every thread that had never had a message.
        self._order = (col, desc, nullsfirst)
        return self

    def limit(self, n):
        self._limit = n
        return self

    @property
    def not_(self):
        return _Not(self)

    # ── execution ────────────────────────────────────────────────────────────

    def _rows(self) -> list:
        return self._store.setdefault(self._table, [])

    def _matching(self) -> list:
        return [r for r in self._rows() if all(f(r) for f in self._filters)]

    def execute(self) -> _Result:
        rows = self._rows()

        if self._op == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            written = []
            for item in payload:
                self._enforce_unique(item, rows)
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                rows.append(row)
                written.append(copy.deepcopy(row))
            return _Result(written)

        if self._op == "upsert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            keys = [k.strip() for k in (self._conflict or "id").split(",")]
            written = []
            for item in payload:
                match = next(
                    (r for r in rows if all(str(r.get(k)) == str(item.get(k)) for k in keys)),
                    None,
                )
                if match is None:
                    row = dict(item)
                    row.setdefault("id", str(uuid.uuid4()))
                    rows.append(row)
                    written.append(copy.deepcopy(row))
                elif not self._ignore_duplicates:
                    match.update(item)
                    written.append(copy.deepcopy(match))
            return _Result(written)

        if self._op == "update":
            touched = []
            for row in self._matching():
                row.update(self._payload)
                touched.append(copy.deepcopy(row))
            return _Result(touched)

        if self._op == "delete":
            doomed = self._matching()
            for row in doomed:
                rows.remove(row)
            return _Result([copy.deepcopy(r) for r in doomed])

        out = self._matching()
        if self._order:
            col, desc, nullsfirst = self._order
            # Postgres's own default: nulls last ascending, nulls first
            # descending. Modelled rather than ignored, because the bug this
            # exists to catch is exactly a caller who forgot it.
            nulls_first = desc if nullsfirst is None else nullsfirst
            present = [r for r in out if r.get(col) is not None]
            missing = [r for r in out if r.get(col) is None]
            present = sorted(present, key=lambda r: str(r.get(col)), reverse=desc)
            out = missing + present if nulls_first else present + missing
        if self._limit is not None:
            out = out[: self._limit]
        return _Result([copy.deepcopy(r) for r in out], count=len(out))


    # ── the one constraint this fake does model ──────────────────────────────
    #
    # Not a general check-constraint engine — see the module note, which is
    # still true. But a *unique* index is different in kind from the rest,
    # because two places in this codebase deliberately use "insert and catch the
    # duplicate" as their concurrency control rather than as an error path:
    # `webhook_log.claim` and `messaging.record_inbound`. The window between a
    # select and an insert is exactly where a provider's near-simultaneous
    # retries land, and the index is the only thing that closes it.
    #
    # A fake that silently accepts the second insert cannot test either of them,
    # and both are money-bearing. So a test can declare the indexes it depends
    # on and get the same 23505 the real database would raise.

    def _enforce_unique(self, item: dict, rows: list) -> None:
        for cols in self._unique.get(self._table, ()):
            if any(item.get(c) is None for c in cols):
                continue  # partial index: `where col is not null`
            clash = any(
                all(str(r.get(c)) == str(item.get(c)) for c in cols) for r in rows
            )
            if clash:
                raise RuntimeError(
                    'duplicate key value violates unique constraint '
                    f'"{self._table}_{"_".join(cols)}_key" (23505)'
                )


class _Not:
    def __init__(self, query: _Query):
        self._q = query

    def in_(self, col, vals):
        wanted = {str(v) for v in vals}
        self._q._filters.append(lambda r, c=col: str(r.get(c)) not in wanted)
        return self._q


@dataclass
class FakeDb:
    """`store` is table name → list of dict rows. Inspect it after a run."""

    store: dict = field(default_factory=dict)
    rpcs: dict = field(default_factory=dict)
    rpc_calls: list = field(default_factory=list)
    # table -> tuples of columns that are unique together. Declared per test:
    # see `_enforce_unique`.
    unique: dict = field(default_factory=dict)
    _codes: itertools.count = field(default_factory=lambda: itertools.count(1801))

    def table(self, name: str) -> _Query:
        return _Query(self.store, name, self.unique)

    def rpc(self, name: str, params: dict | None = None):
        self.rpc_calls.append((name, params or {}))
        value = self.rpcs.get(name)
        if callable(value):
            value = value(params or {})
        if value is None and name == "next_task_code":
            value = f"#{next(self._codes)}"
        return _Rpc(value)

    # ── convenience for tests ────────────────────────────────────────────────

    def rows(self, table: str) -> list:
        return self.store.setdefault(table, [])

    def seed(self, table: str, *rows: dict) -> None:
        self.store.setdefault(table, []).extend(dict(r) for r in rows)


@dataclass
class _Rpc:
    value: object

    def execute(self) -> _Result:
        return _Result(self.value)


def install(monkeypatch, fake: FakeDb, *, extra: tuple[str, ...] = ()) -> FakeDb:
    """
    Point every module that holds a database handle at `fake`.

    **Why this is not a list of module names.** Nearly every fixture in this
    suite carries one, and the codebase has two separate comments recording
    what happens when a name is missing from it: the module quietly reaches the
    real Supabase project, and the test passes or fails for a reason that has
    nothing to do with what it was written to check. One of those cost a session
    — `get_quote` looked up its rate plan in production, found nothing, and the
    session-carryover assertions failed with an unrelated error.

    A list that has to be maintained by hand goes stale the next time somebody
    imports `get_db` somewhere new, which is the §10 lesson about one regex in a
    different room. So the modules are *found* rather than named: anything under
    `app.` already imported that took its own reference to `get_db` gets it
    replaced.

    `extra` is for modules that are not imported yet at fixture time. Passing a
    name that has no `get_db` is harmless.
    """
    import sys

    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for name, module in list(sys.modules.items()):
        if not name.startswith("app.") or module is None:
            continue
        if getattr(module, "get_db", None) is None:
            continue
        monkeypatch.setattr(f"{name}.get_db", lambda: fake, raising=False)
    for name in extra:
        monkeypatch.setattr(f"{name}.get_db", lambda: fake, raising=False)
    return fake
