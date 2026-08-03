"""
Regression tests for /api/v1/hotels pagination stability.

The catalog endpoint feeds the website's sharded sitemaps, which page through
it with limit/offset. Ordering by `review_score` alone is NOT a total order —
it is null for ~17% of rows and heavily tied elsewhere, and Postgres may return
tied rows in any order per query. That made the same offset return different
rows on successive calls, so sitemap shards overlapped and thousands of hotel
pages never made it into any shard.

These tests pin the fix: a unique tiebreaker column, and an exact `total` so
the website can size its shard list.
"""
from unittest.mock import MagicMock

import pytest

from app.api.v1.hotels import endpoint as hotels_endpoint


class _FakeQuery:
    """Records the order()/range() calls the endpoint makes."""

    def __init__(self, recorder):
        self._r = recorder

    def select(self, columns, count=None):
        self._r["select_count"] = count
        return self

    # Filter helpers used by the endpoint — all no-ops that keep chaining.
    @property
    def not_(self):
        return self

    def is_(self, *_args):
        return self

    def ilike(self, *_args):
        return self

    def order(self, column, desc=False, nullsfirst=None):
        self._r["order"].append((column, desc, nullsfirst))
        return self

    def range(self, start, end):
        self._r["range"] = (start, end)
        return self

    def execute(self):
        return MagicMock(data=[], count=7615)


@pytest.fixture
def recorder(monkeypatch):
    rec = {"order": [], "range": None, "select_count": None}
    db = MagicMock()
    db.table.return_value = _FakeQuery(rec)
    monkeypatch.setattr(hotels_endpoint, "get_db", lambda: db)
    return rec


def _call(**kw):
    # __wrapped__ skips the slowapi rate-limit decorator, which insists on a
    # real starlette Request; the query construction is what's under test.
    fn = hotels_endpoint.list_hotels.__wrapped__
    return fn(request=MagicMock(), city=None, destination=None, **kw)


def test_ordering_ends_in_a_unique_tiebreaker(recorder):
    """Without a unique final sort key, tied rows can shuffle between calls."""
    _call(limit=500, offset=0)
    columns = [c for c, _desc, _nf in recorder["order"]]
    assert columns[-1] == "id", f"last ORDER BY key must be unique, got {columns}"
    assert "review_score" in columns


def test_rated_hotels_are_not_buried_behind_nulls(recorder):
    """DESC puts NULLs first in Postgres; crawlers should meet rated stays first."""
    _call(limit=500, offset=0)
    score = next(o for o in recorder["order"] if o[0] == "review_score")
    assert score[1] is True, "review_score should sort descending"
    assert score[2] is False, "nulls must sort last, not first"


def test_range_matches_the_requested_window(recorder):
    _call(limit=500, offset=3000)
    assert recorder["range"] == (3000, 3499)


def test_response_exposes_exact_total_for_shard_sizing(recorder):
    """The website derives its sitemap shard count from `total`."""
    out = _call(limit=1, offset=0)
    assert recorder["select_count"] == "exact"
    assert out["total"] == 7615
    # `count` stays page-length for existing callers.
    assert out["count"] == 0
