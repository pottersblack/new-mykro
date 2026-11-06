import json
from pathlib import Path

import pytest

from gui_flask import paths
from gui_flask.hub import CircuitNotRegistered, CircuitRegistry
from gui_flask.isc_state import default_seed_path
from gui_flask.paths import bundled_resource, config_file, data_dir, ensure_data_dirs, memory_file


def test_data_dir_nonempty():
    p = data_dir('MYKRO_TEST')
    assert isinstance(p, Path)


def test_ensure_dirs(tmp_path, monkeypatch):
    # Simulate local appdata on Windows
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('APPDATA', raising=False)
    d = data_dir('MYKRO_TEST2')
    # ensure_data_dirs should create the directory
    ensure_data_dirs('MYKRO_TEST2')
    assert d == tmp_path / 'MYKRO_TEST2'
    assert d.exists()


def test_source_and_frozen_use_the_windows_user_config(tmp_path, monkeypatch):
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths.sys, "platform", "win32")
    seed = tmp_path / "bundle"
    seed.mkdir()
    (seed / "config.default.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "package_dir", lambda: seed)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    source = config_file("MYKRO_SOURCE_TEST")
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    frozen = config_file("MYKRO_SOURCE_TEST")
    assert source == frozen == tmp_path / "MYKRO_SOURCE_TEST" / "config.json"
    assert memory_file().name == "memories.jsonl"


def test_frozen_config_is_seeded_from_the_default(tmp_path, monkeypatch):
    seed_dir = tmp_path / "bundle" / "gui_flask"
    seed_dir.mkdir(parents=True)
    (seed_dir / "orchestrator_tk.py").write_text("# bundled\n", encoding="utf-8")
    (seed_dir / "config.json").write_text(
        '{"model": {"endpoint": "E:\\\\owner", "api_key": "do-not-copy"}}\n',
        encoding="utf-8",
    )
    (seed_dir / "config.default.json").write_text(
        json.dumps({"setup": {"requires_cloud_api_key": False}, "model": {"endpoint": ""}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "package_dir", lambda: seed_dir)

    dest = config_file("MYKRO_FROZEN_TEST")
    saved = json.loads(dest.read_text(encoding="utf-8"))
    assert dest.parent == tmp_path / "appdata" / "MYKRO_FROZEN_TEST"
    assert saved["setup"]["requires_cloud_api_key"] is False
    assert saved["model"]["endpoint"] == ""
    assert "do-not-copy" not in dest.read_text(encoding="utf-8")
    assert "api_key" not in saved["model"]
    assert config_file("MYKRO_FROZEN_TEST") == dest


def _freeze_at(monkeypatch, meipass: Path) -> None:
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "_MEIPASS", str(meipass), raising=False)


def test_source_bundle_finds_the_registry_next_to_the_package():
    path = bundled_resource("circuit_registry.json")
    assert path.is_file()
    assert path.parent.name == "gui_flask"
    assert "CKT-GW-001" in path.read_text(encoding="utf-8")


def test_frozen_gateway_finds_registry_and_seed_under_meipass(tmp_path: Path, monkeypatch):
    """hub.py is extracted at the archive root; the JSON files are under gui_flask/."""
    meipass = tmp_path / "_MEIPASS"
    bundle = meipass / "gui_flask"
    bundle.mkdir(parents=True)
    (meipass / "hub.py").write_text("# top-level frozen module\n", encoding="utf-8")
    (bundle / "orchestrator_tk.py").write_text("# bundled\n", encoding="utf-8")
    (bundle / "circuit_registry.json").write_text(
        json.dumps(
            {
                "circuits": {
                    "CKT-FROZEN-GW": {
                        "label": "Frozen gateway",
                        "type": "service",
                        "entry_point": "gateway.py",
                        "status": "enabled",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (bundle / "isc_current_seed.json").write_text(
        json.dumps({"__meta__": {"seed_id": "frozen-seed", "generation": 4}}),
        encoding="utf-8",
    )
    _freeze_at(monkeypatch, meipass)

    assert bundled_resource("circuit_registry.json") == bundle / "circuit_registry.json"
    assert default_seed_path() == bundle / "isc_current_seed.json"

    registry = CircuitRegistry(quarantine_dir=str(tmp_path / "logs"))
    registry.require_circuit("CKT-FROZEN-GW")
    with pytest.raises(CircuitNotRegistered):
        registry.require_circuit("CKT-GW-001")


def test_frozen_lookup_also_accepts_files_at_the_archive_root(tmp_path: Path, monkeypatch):
    meipass = tmp_path / "_MEIPASS"
    (meipass / "gui_flask").mkdir(parents=True)
    (meipass / "gui_flask" / "orchestrator_tk.py").write_text("# bundled\n", encoding="utf-8")
    (meipass / "circuit_registry.json").write_text(
        json.dumps(
            {
                "circuits": {
                    "CKT-ROOT": {
                        "label": "Archive root",
                        "type": "service",
                        "entry_point": "gateway.py",
                        "status": "enabled",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _freeze_at(monkeypatch, meipass)

    assert bundled_resource("circuit_registry.json") == meipass / "circuit_registry.json"
    registry = CircuitRegistry(quarantine_dir=str(tmp_path / "logs"))
    registry.require_circuit("CKT-ROOT")
