import json
from pathlib import Path

from gui_flask import paths
from gui_flask.paths import config_file, data_dir, ensure_data_dirs, memory_file


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


def test_source_config_stays_next_to_the_package(monkeypatch):
    monkeypatch.delenv("MYKRO_CONFIG", raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    path = config_file()
    assert path.name == "config.json"
    assert (path.parent / "orchestrator_tk.py").is_file()
    assert memory_file().parent == path.parent


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
