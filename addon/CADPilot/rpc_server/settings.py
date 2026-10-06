"""Persistence of CADPilot server settings under FreeCAD's user app data dir."""

import json
import os
import tempfile

import FreeCAD

_SETTINGS_FILENAME = "cadpilot_settings.json"

_DEFAULT_SETTINGS = {
    "remote_enabled": False,
    "allowed_ips": "127.0.0.1",
    "auto_start_rpc": True,
    "step_panel_visible": False,
    "log_level": "INFO",
    "log_dir": "",
    "log_ring_size": 2000,
}


def _get_settings_path():
    return os.path.join(FreeCAD.getUserAppDataDir(), _SETTINGS_FILENAME)


def load_settings():
    path = _get_settings_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                settings = json.load(f)
            for key, value in _DEFAULT_SETTINGS.items():
                if key not in settings:
                    settings[key] = value
            return settings
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"Failed to load MCP settings: {e}\n")
    return dict(_DEFAULT_SETTINGS)


def save_settings(settings):
    path = _get_settings_path()
    try:
        # tmp + replace (unique tmp): a torn in-place write left corrupt JSON
        # and load_settings then silently reset every setting to defaults.
        fd, tmp_name = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".settings.", suffix=".tmp"
        )
        tmp_path = tmp_name
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(settings, f, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except Exception as e:
        FreeCAD.Console.PrintError(f"Failed to save MCP settings: {e}\n")
