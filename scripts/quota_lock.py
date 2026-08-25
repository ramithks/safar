"""
Two evaluations, one quota, and both of them wrong about why they were slow.

A 560-turn sweep and an eleven-message journey ran against the same Vertex
project at the same time. Neither is concurrent *within itself* — the sweep
takes one turn at a time and the journey is a conversation — so neither had a
concurrency knob to turn down. Together they exhausted the per-region quota
anyway. The transport did what it should: rotated regions, backed off with
jitter, retried. Two turns took 131 and 95 seconds, blew the turn budget, and
escalated.

The measurements from both were then about each other.

**So the bound that was missing is between processes, not inside one.** An
evaluation takes this lock for its whole run; a second one refuses to start and
says what is holding it. Refuses rather than waits, because an evaluation that
silently blocks for two hours is indistinguishable from one that hung, and the
operator who started it should decide.

Advisory and best-effort by design. It is not a mutex over a shared resource —
it cannot be, the quota is Google's and other things use it — it is a guard
against the one contender this repository controls.
"""
import json
import os
import pathlib
import time

LOCK = pathlib.Path(__file__).resolve().parent.parent / ".seed" / "quota.lock"

# A lock older than this is from a process that died. Evaluations are long, so
# the window has to be generous — a six-hour sweep is not a stale lock.
STALE_AFTER_SECONDS = 8 * 3600


class Busy(RuntimeError):
    """Somebody else is already spending the quota."""


def _read() -> dict | None:
    try:
        return json.loads(LOCK.read_text())
    except Exception:  # noqa: BLE001 — absent, empty, or half-written
        return None


def acquire(name: str, *, force: bool = False) -> dict:
    """
    Claim the quota for `name`, or raise `Busy` naming who has it.

    The holder's pid is checked as well as its age: a lock file left behind by
    a killed process would otherwise block every evaluation until somebody
    noticed, and "delete this file" is a worse instruction than a tool that
    works it out.
    """
    LOCK.parent.mkdir(exist_ok=True)
    held = _read()
    if held and not force:
        age = time.time() - (held.get("at") or 0)
        alive = _running(held.get("pid"))
        if alive and age < STALE_AFTER_SECONDS:
            raise Busy(
                f"{held.get('name')} has been running for {int(age // 60)} minutes "
                f"(pid {held.get('pid')}). Two evaluations on one Vertex quota "
                f"measure each other — wait for it, or pass --force if you know "
                f"it is finished."
            )

    mine = {"name": name, "pid": os.getpid(), "at": time.time()}
    LOCK.write_text(json.dumps(mine))
    return mine


def release(name: str) -> None:
    """Give it back, but only if it is still ours."""
    held = _read()
    if held and held.get("pid") == os.getpid():
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass


def _running(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


class held_by:  # noqa: N801 — reads as `with held_by("agent_eval"):`
    def __init__(self, name: str, *, force: bool = False):
        self.name, self.force = name, force

    def __enter__(self):
        return acquire(self.name, force=self.force)

    def __exit__(self, *exc):
        release(self.name)
        return False
