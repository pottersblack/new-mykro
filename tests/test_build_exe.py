from __future__ import annotations

from pathlib import Path

import build_exe
from gui_flask.version import __version__


def test_default_command_is_a_single_console_exe(tmp_path: Path):
    icon = tmp_path / "mykro_icon.ico"
    icon.write_bytes(b"ico")
    version_file = tmp_path / "version_info.txt"
    command = build_exe.build_pyinstaller_command(
        script_path=Path("orch.py"),
        output_name="MYKRO",
        windowed=False,
        onefile=True,
        data_path=tmp_path / "gui_flask",
        icon_file=icon,
        version_file=version_file,
    )
    assert "--onefile" in command
    assert "--onedir" not in command
    assert "--console" in command
    assert "--name" in command
    assert command[command.index("--name") + 1] == "MYKRO"
    assert "--icon" in command
    assert "gui_flask.local_models" in command
    assert "gui_flask.orchestrator_tk" in command
    if build_exe.is_windows():
        assert "--version-file" in command
    else:
        assert "--version-file" not in command


def test_version_file_matches_package_version(tmp_path: Path):
    path = build_exe.write_version_file(tmp_path / "version_info.txt")
    text = path.read_text(encoding="utf-8")
    assert __version__ in text
    assert "MYKRO.exe" in text
    assert "MYKRO local AI harness" in text


def test_packaged_tree_omits_machine_config(tmp_path: Path, monkeypatch):
    source = tmp_path / "gui_flask"
    source.mkdir()
    (source / "config.json").write_text('{"model": {"endpoint": "E:\\\\secret"}}\n', encoding="utf-8")
    (source / "config.default.json").write_text("{}\n", encoding="utf-8")
    (source / "orchestrator_tk.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "notes.log").write_text("log\n", encoding="utf-8")
    monkeypatch.setattr(build_exe, "GUI_DIR", source)
    monkeypatch.setattr(build_exe, "DATA_DIR", tmp_path / "data")
    copied = build_exe.prepare_gui_data()
    assert (copied / "config.default.json").is_file()
    assert (copied / "orchestrator_tk.py").is_file()
    assert not (copied / "config.json").exists()
    assert not (copied / "notes.log").exists()
