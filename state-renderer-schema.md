# State Renderer Snapshot Schema

```json
{
  "session_id": "string",
  "generation_id": "string",
  "request_id": "string",
  "timestamp": "ISO-8601",
  "active_model": "string",
  "selected_route": "string",
  "routing_confidence": "float 0..1",
  "entropy_proxy": "float 0..1",
  "branch_spread": "float 0..1",
  "branch_instability": "float 0..1",
  "top_choice_dominance": "float 0..1",
  "centroid_drift": "float 0..1",
  "memory_pressure": "float 0..1",
  "fallback_proximity": "float 0..1",
  "danger_resemblance_score": "float 0..1",
  "rejected_branch_count": "int >= 0",
  "tool_activity_intensity": "float 0..1",
  "context_density": "float 0..1",
  "prompt_pressure": "float 0..1",
  "render_seed": "int",
  "tags": ["string"],
  "trace": {"compact telemetry metadata"}
}
```

## Field Sources / Proxies

- `session_id`, `generation_id`, `request_id`, `timestamp`: request history + debug trace intake.
- `active_model`, `selected_route`, `routing_confidence`: gateway routing trace.
- `entropy_proxy`: derived from instability + tool intensity + context pressure.
- `branch_spread`: derived from routing candidate breadth + fallback behavior.
- `branch_instability`: fallback influence + backend health instability.
- `top_choice_dominance`: inverse composite of entropy + instability.
- `centroid_drift`: deterministic delta vs prior snapshot in same session.
- `memory_pressure`: injected context size + reflection/archive pressure proxies.
- `fallback_proximity`: fallback influence + unhealthy backend ratio.
- `danger_resemblance_score`: finish reason anomalies + verification failure + fallback proximity.
- `rejected_branch_count`: approval rejects + fallback-induced branch rejections.
- `tool_activity_intensity`: normalized tool call intensity.
- `context_density` / `prompt_pressure`: retained history + injected context block size.
- `render_seed`: deterministic SHA-256-derived integer from quantized telemetry profile.

## Implemented storage and retention behavior

From `gui_flask/state_renderer.py` (`StateRendererStore`):

- Default persistence path: `logs/state_renderer_snapshots.jsonl`
- In-memory structures:
  - per-session ordered store: `_by_session`
  - global deque: `_all`
- Default retention bounds:
  - `max_sessions = 300`
  - `max_per_session = 150`
- Session history API clamp:
  - `list_session(limit)` clamps to `1..200`
- Thread safety:
  - writes/reads guarded by `threading.Lock`
- Persistence failure handling:
  - JSONL append `OSError` is swallowed (non-fatal)

## Lineage resolution behavior

- Snapshot lineage key is `session_id`.
- `get_current(session_id)` returns the latest snapshot for that lineage.
- `get_previous(session_id)` returns the immediately prior snapshot or `None` when lineage depth < 2.

## Privacy / Storage Constraints

- No chain-of-thought persisted.
- No giant raw prompt dumps.
- Compact, bounded, local-first JSON snapshot records only.