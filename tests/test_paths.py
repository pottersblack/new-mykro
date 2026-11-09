import json
import sys
from pathlib import Path

from gui_flask import paths
from gui_flask.paths import config_file, data_dir, ensure_data_dirs


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


def test_source_and_frozen_resolve_the_same_user_config(tmp_path, monkeypatch):
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    source = paths.runtime_config_path()
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    frozen = paths.runtime_config_path()
    assert source == frozen
    assert source == tmp_path / "local" / "MYKRO" / "config.json"
    assert "gui_flask" not in source.parts
    monkeypatch.chdir(tmp_path)
    assert paths.runtime_config_path() == source


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


def test_windows_localappdata_path_is_stable_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Ethan\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\Ethan\AppData\Roaming")
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    text = str(paths.runtime_config_path()).replace("/", "\\")
    assert text == r"C:\Users\Ethan\AppData\Local\MYKRO\config.json"


def test_package_config_json_is_not_the_runtime_file(tmp_path, monkeypatch):
    seed_dir = tmp_path / "bundle" / "gui_flask"
    seed_dir.mkdir(parents=True)
    (seed_dir / "orchestrator_tk.py").write_text("# bundled\n", encoding="utf-8")
    (seed_dir / "config.json").write_text(
        '{"model": {"endpoint": "http://127.0.0.1:1/secret"}}\n',
        encoding="utf-8",
    )
    (seed_dir / "config.default.json").write_text(
        '{"model": {"endpoint": ""}, "setup": {"tier": ""}}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "package_dir", lambda: seed_dir)
    monkeypatch.chdir(tmp_path / "bundle")

    dest = config_file()
    saved = json.loads(dest.read_text(encoding="utf-8"))
    assert dest == (tmp_path / "local" / "MYKRO" / "config.json")
    assert saved["model"]["endpoint"] == ""
    assert "secret" not in dest.read_text(encoding="utf-8")
