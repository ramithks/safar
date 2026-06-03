"""SSE formatting helpers shared across streaming endpoints."""
import json


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def sse_progress(stage: str, pct: int, message: str) -> str:
    return sse_event("progress", {"stage": stage, "progress": pct, "message": message})


def sse_error(message: str) -> str:
    return sse_event("error", {"message": message})
