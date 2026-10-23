MYKRO Neural UI — Notes

Overview

The desktop `orchestrator_tk.py` UI now includes a neural-network inspired visual layer:

- Main nodes are rendered as glowing orbs (constellation-style) with blended colors.
- Links between nodes are drawn and color-blended from the connected nodes.
- "Neural pulses" travel along links to indicate activity (small animated particles).
- Ambient particles and nebula streaks provide atmosphere.

Performance & Controls

All visuals are implemented with the Python standard library (Tkinter) and are configurable via the app Settings (Settings → Save Settings):

- Animation FPS: target frames per second for canvas animation. Lower values reduce CPU usage.
- Low Power Mode: when enabled, ambient particles and pulses are hidden and the pulse spawn rate is reduced to conserve CPU.

Configuration

Settings persist under the `ui` section in the GUI config file (e.g., `gui_flask/config.json`):

{
  "ui": {
    "animation_fps": 24,
    "low_power": false
  }
}

Developer notes

- Pulses are re-used from a small pool to minimize canvas create/delete churn.
- Timing is delta-based so animations scale with actual frame time.
- If you need a "very low" CPU profile, set Animation FPS to 12 and enable Low Power Mode.

If you'd like additional controls (pulse color, intensity, or explicit "disable ambient" toggle), tell me which option you'd like next and I can add it.

Emotion-triggered visual burst

- The UI can now perform a brief, color-coded "emotion explosion" when the assistant emits a strong emotion. This is intended as a rare, short (≈1–2s) visual accent and respects Low Power Mode and neural-lock safety.
- Emotion → color mapping (defaults): anger: red (#ff3b30), excited: orange (#ff9500), joy: yellow (#ffd60a), sadness: blue (#007aff), fear: purple (#6a0dad), surprise: magenta (#ff2d55), disgust: teal (#1abc9c), neutral: gray (#9aa0a6).
- How emotions are detected: the GUI will first try a configured small model (configured via `ui.emotion_model` or `ui.small_model` in config). If none is available, a lightweight keyword-based classifier is used as a fallback.
- Developer preview: open Settings → "Emotion Preview" to pick an emotion and preview the burst; there's also a keyboard shortcut Ctrl+E to trigger the preview.

If you want the explosion to be more or less intense, or to include particle bursts instead of the radial flash, I can add tunable controls (duration, particle count, intensity) and an API hook for programs to trigger it directly.
