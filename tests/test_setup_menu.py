"""One-command A/B/C/D setup. No installer or model download runs for real."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from gui_flask.launch_autoconfig import install_ollama_command
from gui_flask.linux_control import LinuxPort, parse_wmctrl_lpG, parse_xrandr_size
from gui_flask.setup_menu import (
    TIERS,
    build_plan,
    execute_plan,
    main,
    model_present,
    parse_tier,
    parse_yes_no,
    render_menu,
    windows_shortcut_command,
    write_linux_desktop_files,
)
from gui_flask.win_control import SW_MINIMIZE, WindowControl, WindowQuery, default_window_port


ROOT = Path(__file__).resolve().parents[1]
TAGS = "http://127.0.0.1:11434/api/tags"


def _no_cloud(commands: list[list[str]]) -> None:
    blob = " ".join(" ".join(command) for command in commands).casefold()
    assert "api_key" not in blob
    assert "huggingface" not in blob
    assert "openai.com" not in blob
    assert "hf_token" not in blob


def test_menu_offers_four_local_choices_and_no_cloud_key():
    text = render_menu()
    for tier_id, tier in TIERS.items():
        assert tier_id in text
        assert tier.title in text
        assert tier.model in text
    assert "No cloud API key." in text
    assert "release" not in text.casefold()
    assert parse_tier("a") == "A"
    assert parse_tier("D.") == "D"
    assert parse_tier("B stronger") == "B"
    assert parse_tier("all") is None
    assert parse_tier("") is None


def test_tiers_pull_larger_local_models_and_d_is_linux_only():
    assert TIERS["A"].model == "qwen2.5-coder:1.5b"
    assert TIERS["B"].model == "qwen2.5-coder:7b"
    assert TIERS["C"].model == "qwen2.5-coder:14b"
    assert TIERS["D"].linux_only is True
    assert TIERS["D"].model == "qwen2.5-coder:7b"
    windows = install_ollama_command("windows")
    linux = install_ollama_command("linux")
    assert "Ollama.Ollama" in windows
    assert "winget" not in linux
    assert "install.sh" in " ".join(linux)


def test_windows_plan_downloads_the_small_model_without_wiring(tmp_path: Path):
    plan = build_plan(
        "A",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path=None,
        venv_ready=False,
    )
    assert plan.model == "qwen2.5-coder:1.5b"
    assert plan.apt == []
    assert plan.create_venv[:3] == ["py", "-m", "venv"]
    assert plan.install_ollama[0] == "winget"
    assert plan.pull == ["ollama", "pull", "qwen2.5-coder:1.5b"]
    assert plan.serve == ["ollama", "serve"]
    assert plan.launch == [str(tmp_path / "dist" / "MYKRO.exe")]
    assert plan.build[-1].endswith("build_exe.py")
    assert "--onedir" not in plan.build
    assert plan.pip_build[-1] == "requirements-build.txt"
    assert plan.desktop_icon is False
    assert plan.endpoint.startswith("http://127.0.0.1:11434")
    assert [command[-1] for command in plan.pip] == ["requirements.txt"]
    _no_cloud([plan.install_ollama, plan.pull, plan.serve, plan.build, plan.launch])


def test_stronger_presets_change_only_the_model(tmp_path: Path):
    medium = build_plan(
        "B",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path=r"C:\Ollama\ollama.exe",
        venv_ready=True,
    )
    heavy = build_plan(
        "C",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path=r"C:\Ollama\ollama.exe",
        venv_ready=True,
    )
    assert medium.install_ollama == []
    assert medium.pull[-1] == "qwen2.5-coder:7b"
    assert heavy.pull[-1] == "qwen2.5-coder:14b"
    assert medium.serve[0] == heavy.serve[0] == r"C:\Ollama\ollama.exe"
    assert medium.create_venv == []


def test_windows_refuses_the_linux_tier(tmp_path: Path):
    refused = build_plan(
        "D",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path=None,
        venv_ready=False,
    )
    assert refused.message.startswith("D is the Linux version")
    assert "bash setup.sh" in refused.message


def test_linux_tier_installs_desktop_tools_and_a_local_model(tmp_path: Path):
    plan = build_plan(
        "D",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path=None,
        venv_ready=False,
        root=False,
    )
    assert plan.apt[:3] == ["sudo", "apt-get", "install"]
    assert "python3-tk" in plan.apt
    assert "wmctrl" in plan.apt
    assert "xdotool" in plan.apt
    assert plan.install_ollama[0] == "sh"
    assert "install.sh" in plan.install_ollama[-1]
    assert plan.pull[-1] == "qwen2.5-coder:7b"
    assert plan.launch == [str(tmp_path / "dist" / "MYKRO")]
    assert not plan.executable.endswith(".exe")
    assert "binutils" in plan.apt
    assert any(command[-1] == "requirements-linux.txt" for command in plan.pip)
    _no_cloud([plan.apt, plan.install_ollama, plan.pull])

    small = build_plan(
        "A",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path="/usr/local/bin/ollama",
        venv_ready=True,
        root=True,
    )
    assert "wmctrl" not in small.apt
    assert "xdotool" not in small.apt
    assert "python3-tk" in small.apt
    assert small.apt[0] == "apt-get"
    assert small.install_ollama == []
    assert small.pull == ["/usr/local/bin/ollama", "pull", "qwen2.5-coder:1.5b"]


def test_execute_installs_pulls_saves_and_launches(tmp_path: Path):
    plan = build_plan(
        "A",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path=None,
        venv_ready=True,
        root=True,
    )
    commands: list[list[str]] = []
    spawned: list[list[str]] = []
    launched: list[list[str]] = []

    def run(argv: list[str]) -> tuple[bool, str]:
        commands.append(list(argv))
        return True, ""

    def opener(url: str, timeout: float):
        del timeout
        if url != TAGS:
            return None
        if any(len(command) >= 2 and command[1] == "pull" for command in commands):
            return {"models": [{"name": "qwen2.5-coder:1.5b"}]}
        if spawned:
            return {"models": []}
        return None

    config_path = tmp_path / "config.json"
    result = execute_plan(
        plan,
        config_path=config_path,
        run=run,
        spawn=spawned.append,
        opener=opener,
        launch=lambda argv: launched.append(argv) or 0,
        locate_ollama=lambda: "/usr/bin/ollama",
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
    )
    assert result.ok is True
    assert result.launched is True
    assert spawned == [["/usr/bin/ollama", "serve"]]
    assert ["ollama", "pull", "qwen2.5-coder:1.5b"] in commands or [
        "/usr/bin/ollama",
        "pull",
        "qwen2.5-coder:1.5b",
    ] in commands
    assert launched == [plan.launch]
    assert plan.launch == [str(tmp_path / "dist" / "MYKRO")]
    assert any(command[-1].endswith("build_exe.py") for command in commands)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["backend"] == "ollama"
    assert saved["model"]["model_name"] == "qwen2.5-coder:1.5b"
    assert saved["model"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert saved["setup"]["tier"] == "A"
    assert saved["setup"]["requires_cloud_api_key"] is False
    assert "api_key" not in saved
    assert "openai_api_key" not in saved
    assert "api_key" not in saved["model"]
    _no_cloud(commands)


def test_execute_skips_pull_when_the_model_is_already_local(tmp_path: Path):
    plan = build_plan(
        "B",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path="/usr/bin/ollama",
        venv_ready=True,
        root=True,
    )
    commands: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == TAGS:
            return {"models": [{"name": "qwen2.5-coder:7b"}]}
        return None

    result = execute_plan(
        plan,
        config_path=tmp_path / "config.json",
        run=lambda argv: commands.append(argv) or (True, ""),
        spawn=lambda _argv: (_ for _ in ()).throw(AssertionError("server already up")),
        opener=opener,
        launch=lambda _argv: 0,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
        do_launch=False,
    )
    assert result.ok is True
    assert result.launched is False
    assert all("pull" not in command for command in commands)
    assert model_present({"models": [{"name": "qwen2.5-coder:7b"}]}, "qwen2.5-coder:7b")


def test_linux_pip_falls_back_when_the_full_list_fails(tmp_path: Path):
    plan = build_plan(
        "A",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path="/usr/bin/ollama",
        venv_ready=True,
        root=True,
    )
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[bool, str]:
        seen.append(list(argv))
        if argv[-1] == "requirements.txt":
            return False, "pynput failed to build"
        return True, ""

    def opener(url: str, timeout: float):
        del timeout
        if url == TAGS:
            return {"models": [{"name": "qwen2.5-coder:1.5b"}]}
        return None

    result = execute_plan(
        plan,
        config_path=tmp_path / "config.json",
        run=run,
        spawn=lambda _argv: None,
        opener=opener,
        launch=None,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
        do_launch=False,
    )
    assert result.ok is True
    pip = [command for command in seen if "pip" in command and "-m" in command]
    assert [command[-1] for command in pip] == [
        "requirements.txt",
        "requirements-linux.txt",
        "requirements-build.txt",
    ]


def test_failed_download_does_not_launch(tmp_path: Path):
    plan = build_plan(
        "C",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path="ollama",
        venv_ready=True,
    )
    launched: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == TAGS:
            return {"models": []}
        return None

    result = execute_plan(
        plan,
        config_path=tmp_path / "config.json",
        run=lambda argv: (False, "disk full") if "pull" in argv else (True, ""),
        spawn=lambda _argv: None,
        opener=opener,
        launch=lambda argv: launched.append(argv) or 0,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
    )
    assert result.ok is False
    assert "disk full" in result.message
    assert launched == []
    assert not (tmp_path / "config.json").exists()


def test_menu_dry_run_and_noninteractive_tier(tmp_path: Path):
    printed: list[str] = []
    code = main(
        ["--tier", "b", "--dry-run"],
        platform="windows",
        repo_root=tmp_path,
        python="py",
        print_fn=printed.append,
        locate_ollama=lambda: None,
        venv_ready=True,
        isatty=False,
    )
    assert code == 0
    text = "\n".join(printed)
    assert "qwen2.5-coder:7b" in text
    assert "winget" in text
    assert "build_exe.py" in text
    assert "MYKRO.exe" in text
    assert "Desktop icon: no" in text

    refused: list[str] = []
    code = main(
        ["--tier", "D", "--dry-run"],
        platform="windows",
        repo_root=tmp_path,
        print_fn=refused.append,
        locate_ollama=lambda: None,
        venv_ready=True,
        isatty=False,
    )
    assert code == 2
    assert "bash setup.sh" in "\n".join(refused)

    quiet: list[str] = []
    code = main(
        [],
        platform="linux",
        repo_root=tmp_path,
        print_fn=quiet.append,
        isatty=False,
    )
    assert code == 2
    assert "Pass --tier" in "\n".join(quiet)


def test_interactive_choice_runs_the_selected_tier(tmp_path: Path):
    answers = iter(["nope", "C", "n"])
    printed: list[str] = []
    launched: list[list[str]] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == TAGS:
            return {"models": [{"name": "qwen2.5-coder:14b"}]}
        return None

    code = main(
        ["--no-launch"],
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        config_path=tmp_path / "config.json",
        input_fn=lambda _prompt: next(answers),
        print_fn=printed.append,
        isatty=True,
        locate_ollama=lambda: "/usr/bin/ollama",
        venv_ready=True,
        root=True,
        run=lambda _argv: (True, ""),
        spawn=lambda _argv: None,
        opener=opener,
        launcher=lambda argv: launched.append(argv) or 0,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
    )
    assert code == 0
    assert launched == []
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["setup"]["tier"] == "C"
    assert saved["model"]["model_name"] == "qwen2.5-coder:14b"
    assert "Enter A, B, C, or D." in "\n".join(printed)
    assert "Create a desktop icon for MYKRO?" in "\n".join(printed)


def test_wrappers_are_the_one_command():
    powershell = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "setup.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "setup.py" in powershell
    assert "Python.Python.3.12" in powershell
    assert "setup.py" in shell
    assert "python3" in shell
    assert "setup.ps1" in readme
    assert "setup.sh" in readme
    assert "latest release" not in readme.casefold()
    assert "MYKRO-windows" not in readme
    assert "desktop icon" in readme.casefold()
    assert "dist\\MYKRO.exe" in readme or "dist/MYKRO.exe" in readme
    assert "dist/MYKRO" in readme


def test_icon_choice_builds_one_file_and_still_launches_if_the_icon_fails(tmp_path: Path):
    plan = build_plan(
        "A",
        platform="windows",
        repo_root=tmp_path,
        python="py",
        ollama_path="ollama",
        venv_ready=True,
        desktop_icon=True,
    )
    assert plan.executable.endswith("MYKRO.exe")
    assert plan.desktop_icon is True
    shortcut = windows_shortcut_command(tmp_path, Path(plan.executable))
    assert shortcut[0] == "powershell"
    assert "create_mykro_shortcut.ps1" in shortcut[5]
    assert shortcut[-1].endswith("MYKRO.exe")

    launched: list[list[str]] = []
    icons: list[str] = []

    def opener(url: str, timeout: float):
        del timeout
        if url == TAGS:
            return {"models": [{"name": "qwen2.5-coder:1.5b"}]}
        return None

    result = execute_plan(
        plan,
        config_path=tmp_path / "config.json",
        run=lambda argv: (False, "pyinstaller failed") if str(argv[-1]).endswith("build_exe.py") else (True, ""),
        spawn=lambda _argv: None,
        opener=opener,
        launch=lambda argv: launched.append(argv) or 0,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
        place_icon=lambda chosen: icons.append(chosen.executable) or (True, "icon"),
    )
    assert result.ok is False
    assert "pyinstaller failed" in result.message
    assert launched == []
    assert icons == []

    built = build_plan(
        "B",
        platform="linux",
        repo_root=tmp_path,
        python="python3",
        ollama_path="/usr/bin/ollama",
        venv_ready=True,
        root=True,
        desktop_icon=True,
    )
    launched.clear()
    result = execute_plan(
        built,
        config_path=tmp_path / "built.json",
        run=lambda _argv: (True, ""),
        spawn=lambda _argv: None,
        opener=lambda url, _timeout: {"models": [{"name": "qwen2.5-coder:7b"}]} if url == TAGS else None,
        launch=lambda argv: launched.append(argv) or 0,
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        serve_timeout=0,
        place_icon=lambda _chosen: (False, "desktop folder missing"),
    )
    assert result.ok is True
    assert result.launched is True
    assert launched == [built.launch]


def test_linux_desktop_file_points_at_the_binary(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/Desktop"\n',
        encoding="utf-8",
    )
    exe = tmp_path / "dist" / "MYKRO"
    exe.parent.mkdir()
    exe.write_text("binary\n", encoding="utf-8")
    path = write_linux_desktop_files(home, exe, tmp_path / "tools" / "mykro_icon.ico")
    assert path == home / "Desktop" / "MYKRO.desktop"
    text = path.read_text(encoding="utf-8")
    assert f"Exec={exe}" in text
    assert "Name=MYKRO" in text
    assert "api_key" not in text
    assert "huggingface" not in text.casefold()
    apps = home / ".local" / "share" / "applications" / "MYKRO.desktop"
    assert apps.read_text(encoding="utf-8") == text
    assert parse_yes_no("y") is True
    assert parse_yes_no("NO") is False
    assert parse_yes_no("maybe") is None


def test_dry_run_can_request_the_desktop_icon(tmp_path: Path):
    printed: list[str] = []
    code = main(
        ["--tier", "A", "--icon", "--dry-run"],
        platform="windows",
        repo_root=tmp_path,
        python="py",
        print_fn=printed.append,
        locate_ollama=lambda: r"C:\Ollama\ollama.exe",
        venv_ready=True,
        isatty=False,
    )
    assert code == 0
    text = "\n".join(printed)
    assert "Desktop icon: yes" in text
    assert "dist" in text and "MYKRO.exe" in text
    conflict: list[str] = []
    code = main(
        ["--tier", "A", "--icon", "--no-icon"],
        platform="linux",
        repo_root=tmp_path,
        print_fn=conflict.append,
        locate_ollama=lambda: "/usr/bin/ollama",
        venv_ready=True,
        isatty=False,
    )
    assert code == 2
    assert "either --icon or --no-icon" in "\n".join(conflict)


def test_linux_window_port_parses_targets_and_focuses():
    text = "0x000004d2  0 4242  10  20  800 600  box  Code — Editor\n"
    records = parse_wmctrl_lpG(text)
    assert len(records) == 1
    assert records[0].hwnd == 1234
    assert records[0].pid == 4242
    assert records[0].title == "Code — Editor"
    assert records[0].right == 810
    assert records[0].bottom == 620
    assert parse_xrandr_size("eDP-1 connected 1920x1080+0+0").right == 1920

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[:2] == ["wmctrl", "-lpG"]:
            return 0, text
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return 0, "1234\n"
        return 0, ""

    port = LinuxPort(runner=runner)
    assert port.secure_desktop() is False
    control = WindowControl(port, attempts=1, sleeper=lambda _seconds: None)
    ok, _message, record = control.focus(WindowQuery(title="Editor"))
    assert ok is True
    assert record is not None
    assert record.pid == 4242
    assert ["wmctrl", "-ia", hex(1234)] in calls
    port.show(1234, SW_MINIMIZE)
    assert ["xdotool", "windowminimize", "1234"] in calls


def test_linux_default_port_is_not_win32():
    if not sys.platform.startswith("linux"):
        return
    port = default_window_port()
    assert isinstance(port, LinuxPort)
    assert port.secure_desktop() is False
