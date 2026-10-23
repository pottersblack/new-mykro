# State Renderer Visual Mapping

## Mapping Rules

Implemented in `gui_flask/templates/neural_index.html` inside `drawStateSnapshotFrame(now)`.

- `entropy_proxy` → turbulence amplitude + diffusion fog density.
- `top_choice_dominance` → central core brightness/intensity.
- `branch_spread` → number of lobed plumes.
- `centroid_drift` → directional field drift velocity.
- `memory_pressure` → particle density/compression and occlusion weight.
- `fallback_proximity` → edge instability pulse thickness.
- `rejected_branch_count` → ghost wisps/tendrils count.
- `danger_resemblance_score` → warning hue contamination and border distortion pulse.
- `tool_activity_intensity` → impulse rings/burst frequency.

## Determinism

- Randomness is seeded by `render_seed` only.
- Geometry/camera remains fixed between Current and Previous modes.
- Same snapshot always renders same semantic signature.

## Legibility Constraints

- Diagnostic first, artistic second.
- Stable framing for comparisons.
- Meaningful separation between low-entropy/high-dominance and high-instability/high-danger states.

## Compare-Mode Readiness

Current implementation uses one render viewport. Compare mode is future-ready only (not implemented yet), but the existing mapping pipeline can be reused for:

- side-by-side dual canvases
- overlay blending
- swipe compare
- metric-diff highlighting.