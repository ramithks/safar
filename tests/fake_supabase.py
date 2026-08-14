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
    def __init__(self, store: dict, table: str):
        self._store = store
        self._table = table
        self._filters: list = []
        self._op = "select"
        self._payload = None
        self._order: tuple[str, bool] | None = None
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

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: str(r.get(c)) != str(v))
        return self

    def in_(self, col, vals):
        wanted = {str(v) for v in vals}
        self._filters.append(lambda r, c=col: str(r.get(c)) in wanted)
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

    def order(self, col, desc=False):
        self._order = (col, desc)
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
            col, desc = self._order
            out = sorted(out, key=lambda r: str(r.get(col) or ""), reverse=desc)
        if self._limit is not None:
            out = out[: self._limit]
        return _Result([copy.deepcopy(r) for r in out], count=len(out))


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
    _codes: itertools.count = field(default_factory=lambda: itertools.count(1801))

    def table(self, name: str) -> _Query:
        return _Query(self.store, name)

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
