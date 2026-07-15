"""
Patch protocol — how agents (and users) mutate TripState.

A patch is a small JSON-Patch-style op targeting a slash-separated path into
the TripState dict ("/transport/going", "/itinerary/days/2", "/costs/total").
List indices are numeric segments; "-" appends.

Ops:
  set    — replace (or create) the value at path
  merge  — shallow-merge a dict into the dict at path
  remove — delete the key / list element at path
  append — append value to the list at path

Ownership: each agent may only write under its declared path prefixes.
The orchestrator calls validate_ownership() before applying agent patches;
`user` and `supervisor` actors are unrestricted.
"""
import logging
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PatchOp(BaseModel):
    op: Literal["set", "merge", "remove", "append"]
    path: str
    value: Any = None


# Agent → path prefixes it may write. Prefix match on whole segments.
AGENT_OWNERSHIP: dict[str, list[str]] = {
    "journey":        ["/transport", "/documents", "/destination/currency_info"],
    "itinerary":      ["/itinerary", "/destination"],
    "hotel":          ["/stay", "/transport/going/hotels"],
    "budget":         ["/costs"],
    "recommendation": ["/stay", "/transport"],
    "weather":        ["/weather"],
    "supervisor":     ["/"],   # may touch anything (merge decisions, meta)
    "user":           ["/"],
}


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def validate_ownership(actor: str, patches: list[PatchOp]) -> None:
    """Raise PermissionError if any patch writes outside the actor's fields."""
    prefixes = AGENT_OWNERSHIP.get(actor)
    if prefixes is None:
        raise PermissionError(f"Unknown actor '{actor}'")
    if "/" in prefixes:
        return
    for p in patches:
        norm = "/" + "/".join(_segments(p.path))
        if not any(norm == pre or norm.startswith(pre + "/") for pre in prefixes):
            raise PermissionError(
                f"Agent '{actor}' attempted out-of-scope write to '{p.path}' "
                f"(owns: {prefixes})"
            )


def _walk(container: Any, segs: list[str], create: bool) -> tuple[Any, str]:
    """Walk to the parent of the final segment; return (parent, final_key)."""
    cur = container
    for i, seg in enumerate(segs[:-1]):
        if isinstance(cur, list):
            cur = cur[int(seg)]
        elif isinstance(cur, dict):
            if seg not in cur:
                if not create:
                    raise KeyError("/" + "/".join(segs[: i + 1]))
                cur[seg] = {}
            cur = cur[seg]
        else:
            raise TypeError(
                f"Cannot traverse '{seg}' — parent at "
                f"'/{'/'.join(segs[:i])}' is {type(cur).__name__}"
            )
    return cur, segs[-1]


def apply_patch(state: dict, patch: PatchOp) -> dict:
    """Apply one patch to a TripState dict in place. Returns state."""
    segs = _segments(patch.path)
    if not segs:
        if patch.op == "merge" and isinstance(patch.value, dict):
            state.update(patch.value)
            return state
        raise ValueError(f"Op '{patch.op}' not allowed on root path")

    parent, key = _walk(state, segs, create=patch.op in ("set", "merge", "append"))

    if patch.op == "set":
        if isinstance(parent, list):
            if key == "-":
                parent.append(patch.value)
            else:
                parent[int(key)] = patch.value
        else:
            parent[key] = patch.value

    elif patch.op == "merge":
        target = parent[int(key)] if isinstance(parent, list) else parent.get(key)
        if target is None and isinstance(parent, dict):
            parent[key] = patch.value if isinstance(patch.value, dict) else {}
        elif isinstance(target, dict) and isinstance(patch.value, dict):
            target.update(patch.value)
        else:
            raise TypeError(f"merge requires dicts at '{patch.path}'")

    elif patch.op == "remove":
        if isinstance(parent, list):
            del parent[int(key)]
        else:
            parent.pop(key, None)

    elif patch.op == "append":
        if isinstance(parent, dict) and key not in parent:
            parent[key] = []
        target = parent[int(key)] if isinstance(parent, list) else parent[key]
        if not isinstance(target, list):
            raise TypeError(f"append requires a list at '{patch.path}'")
        target.append(patch.value)

    return state


def apply_patches(state: dict, patches: list[PatchOp], actor: str = "supervisor") -> dict:
    """Validate ownership then apply patches in order. Mutates and returns state."""
    validate_ownership(actor, patches)
    for p in patches:
        apply_patch(state, p)
    return state
