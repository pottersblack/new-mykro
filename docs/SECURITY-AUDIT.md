# Security & Privacy Audit (automated scan)

This file summarizes sensitive files and patterns discovered in the repository during the packaging work.

Findings (high-level)
- `.env` — project root: may contain local secrets or credentials (exists as template).
- `gui_flask/memories.jsonl`, `gui_flask/episodes.jsonl`, `logs/` — these JSONL logs and runtime stores may contain user data, model traces, or personally identifiable information and should not be committed to a public repo.
- `gui_flask/config.json` — machine runtime config. It is gitignored. The public template is `gui_flask/config.default.json`.
- `logs/*.jsonl` and `logs/archive/` — historical logs may contain absolute local paths and must not be committed.

Exact paths discovered (representative sample):
- gui_flask/memories.jsonl
- gui_flask/episodes.jsonl
- gui_flask/isc_current_seed.json
- gui_flask/config.json
- logs/wiring_hub.jsonl
- logs/archive/**
- .env

Recommendations
1. Remove any secrets from tracked files immediately. If secrets exist in history, rotate them and purge via git-filter-repo or BFG.
2. Add runtime artifacts to `.gitignore` (done): `*.jsonl`, `logs/`, `dist/`, `build/`, `*.exe`.
3. Replace tracked runtime data with safe seeded fixtures or templates (for example `config.example.json`) and ensure the live `config.json` is created via autoconfig or developer setup.
4. Consider adding a pre-commit hook to detect accidental secrets (detect-secrets / truffleHog) in CI.

If you want, I can prepare a safe `git filter-repo` command and a migration plan to remove sensitive data from repository history.
