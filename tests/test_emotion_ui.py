from __future__ import annotations

import sys
from pathlib import Path

# Ensure gui_flask is on the path (same pattern as other tests)
sys.path.insert(0, str(Path(__file__).parent.parent / "gui_flask"))

import pytest

from orchestrator_tk import AppGUI


def test_local_emotion_classifier():
    cls = AppGUI._local_emotion_classifier
    assert cls(None, "I am so happy and full of joy!") == "joy"
    assert cls(None, "I am furious and mad") == "anger"
    assert cls(None, "This is disgusting, yuck") == "disgust"
    assert cls(None, "I am surprised, wow") == "surprise"
    assert cls(None, "I am afraid and nervous") == "fear"
    assert cls(None, "I'm excited and pumped") == "excited"
    assert cls(None, "Feeling sad and unhappy") == "sadness"
    assert cls(None, "") == "neutral"


def test_trigger_emotion_visual_dummy_self_no_error():
    class DummyCanvas:
        def __init__(self):
            self.ids = set()
            self._next = 1

        def create_oval(self, *args, **kwargs):
            nid = self._next
            self._next += 1
            self.ids.add(nid)
            return nid

        def coords(self, *args, **kwargs):
            pass

        def itemconfigure(self, *args, **kwargs):
            pass

        def delete(self, item_id):
            self.ids.discard(item_id)

        def winfo_width(self):
            return 1200

        def winfo_height(self):
            return 800

    class DummySelf:
        def __init__(self):
            self.canvas = DummyCanvas()

        def after(self, ms, fn):
            # call immediately for test determinism
            fn()

        def _panel_log(self, msg):
            pass

    ds = DummySelf()
    # Should not raise
    AppGUI._trigger_emotion_visual(ds, "joy")
    AppGUI._trigger_emotion_visual(ds, "anger", subdued=True)


def test_handle_assistant_emotion_triggers():
    class DummyConfig:
        def get(self, k, default=None):
            return {}

    class DummyHub:
        def __init__(self):
            self.config = DummyConfig()
            self.model_clients = {}

    class DummySelf:
        def __init__(self):
            self.hub = DummyHub()
            self.temp_lock_active = False
            self.low_power_mode = False
            self.triggered = []

        def after(self, ms, fn):
            fn()

        def _trigger_emotion_visual(self, emotion, subdued=False):
            self.triggered.append((emotion, subdued))
        
        def _local_emotion_classifier(self, text: str) -> str:
            # delegate to the AppGUI implementation for consistency
            return AppGUI._local_emotion_classifier(None, text)

    ds = DummySelf()
    AppGUI._handle_assistant_emotion(ds, "I'm excited and pumped")
    assert ds.triggered, "Expected trigger for excited"
    assert ds.triggered[0][0] in ("excited", "joy")

    # low power: only anger/sadness produce subdued flash
    ds2 = DummySelf()
    ds2.low_power_mode = True
    AppGUI._handle_assistant_emotion(ds2, "I am angry and furious")
    assert ds2.triggered and ds2.triggered[0][0] == "anger" and ds2.triggered[0][1] is True

    ds3 = DummySelf()
    ds3.low_power_mode = True
    AppGUI._handle_assistant_emotion(ds3, "I am excited")
    assert ds3.triggered == []

    # temp lock disables entirely
    ds4 = DummySelf()
    ds4.temp_lock_active = True
    AppGUI._handle_assistant_emotion(ds4, "I am furious")
    assert ds4.triggered == []
