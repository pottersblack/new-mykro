from __future__ import annotations

import collections
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TimelineEvent:
    request_id: str
    event_type: str
    component: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TimelineBus:
    """Thread-safe per-request event timeline with JSONL persistence."""

    def __init__(self, log_path: str | None = None, max_requests: int = 200, max_events_per_request: int = 200):
        if log_path is None:
            log_path = str((Path(__file__).resolve().parent.parent / "logs" / "request_timeline.jsonl").resolve())
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._events: collections.OrderedDict[str, list[dict[str, Any]]] = collections.OrderedDict()
        self._max_requests = max(10, int(max_requests))
        self._max_events_per_request = max(20, int(max_events_per_request))

    def emit(self, request_id: str, event_type: str, component: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        event = TimelineEvent(
            request_id=request_id,
            event_type=event_type,
            component=component,
            data=data or {},
        ).to_dict()
        with self._lock:
            bucket = self._events.setdefault(request_id, [])
            bucket.append(event)
            if len(bucket) > self._max_events_per_request:
                del bucket[:-self._max_events_per_request]
            self._events.move_to_end(request_id)
            while len(self._events) > self._max_requests:
                self._events.popitem(last=False)
            try:
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, default=str) + "\n")
            except OSError:
                pass
        return event

    def get_events(self, request_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(request_id, []))

    def list_recent(self, limit: int = 25) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit))
        with self._lock:
            flattened: list[dict[str, Any]] = []
            for events in reversed(self._events.values()):
                flattened.extend(reversed(events))
                if len(flattened) >= safe_limit:
                    break
        return flattened[:safe_limit]


_timeline_state: dict[str, TimelineBus | None] = {"instance": None}
_timeline_lock = threading.Lock()


def get_timeline_bus(log_path: str | None = None) -> TimelineBus:
    if _timeline_state["instance"] is None:
        with _timeline_lock:
            if _timeline_state["instance"] is None:
                _timeline_state["instance"] = TimelineBus(log_path=log_path)
    return _timeline_state["instance"]
