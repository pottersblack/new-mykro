## Phase A: Baseline Validation Evidence (Path-Correction Pass)

### Validation timestamp
- Date: `2026-06-01`
- OS: `Windows`
- Workspace root: `the repository root`

### Canonical corrected command paths
1. `python -m pytest tests/test_web_ui_contracts.py -v -k session`
2. `python tools/state_renderer_store_validation.py`
3. `python tools/state_renderer_api_validation.py`
4. `python tests/smoke_test.py`

> Execution note: to ensure dependency parity with the repo environment, commands were executed with workspace interpreter `the repository root\.venv\Scripts\python.exe` and the same corrected script paths.

### A-1: Session contract baseline
- Command run: `& "the repository root\.venv\Scripts\python.exe" -m pytest tests/test_web_ui_contracts.py -v -k session`
- Result: **PASS**
- Key evidence:
  - `collected 11 items / 5 deselected / 6 selected`
  - `tests/test_web_ui_contracts.py::test_api_completion_emits_structured_session_trace_events PASSED`
  - `tests/test_web_ui_contracts.py::test_multi_turn_edge_flow_preserves_history_session_and_review_cadence PASSED`
  - `6 passed, 5 deselected in 1.44s`
  - `exit:0`

### A-2: State-renderer store contract baseline
- Command run: `& "the repository root\.venv\Scripts\python.exe" tools/state_renderer_store_validation.py`
- Result: **PASS**
- Key evidence:
  - `current request_id: req-a3`
  - `previous request_id: req-a2`
  - `replay current request_id: req-a3`
  - `replay previous request_id: req-a2`
  - `jsonl path: the repository root\logs\state_renderer_store_validation.jsonl`
  - `exit:0`

### A-3: State-renderer API contract baseline
- Command run: `& "the repository root\.venv\Scripts\python.exe" tools/state_renderer_api_validation.py`
- Result: **PASS**
- Key evidence:
  - `gateway_current_request_id: req-multi-2`
  - `gateway_previous_request_id: req-multi-1`
  - `api_validation: PASS`
  - `exit:0`

### A-4: Smoke test baseline
- Command run: `& "the repository root\.venv\Scripts\python.exe" tests/smoke_test.py`
- Result: **PASS**
- Key evidence:
  - `SMOKE TEST PASSED`
  - `DESKTOP AGENT TESTS PASSED`
  - `MemoryStore tests PASSED`
  - `ALL TESTS PASSED`
  - `exit:0`

### Summary
- Total validations run: **4**
- Total passed: **4**
- Total failed: **0**
- Total blocked: **0**
- Phase A gate status: **GREEN**
- Promotion note: all Phase A baseline validations passed with real rerun evidence.
