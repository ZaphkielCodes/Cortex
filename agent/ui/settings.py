import json
import os
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent.parent / "ui_settings.json"

DEFAULT_SETTINGS = {
    "orb_enabled": True,
    "opacity": 0.9,
    "size": 60,
    "position": {"x": -1, "y": -1},  # -1 means default corner
    "corner": "bottom_right",
    "monitor": 0,
    "always_on_top": True,
    "show_when_idle": True,
    "wake_word_enabled": True,
    "visual_feedback_enabled": True
}

class UISettings:
    def __init__(self):
        self._settings = DEFAULT_SETTINGS.copy()
        self.load()

    def load(self):
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
                    self._settings.update(data)
            except Exception:
                pass

    def save(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(self._settings, f, indent=4)
        except Exception:
            pass

    def get(self, key, default=None):
        return self._settings.get(key, default)

    def set(self, key, value):
        self._settings[key] = value
        self.save()
