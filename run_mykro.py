from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / "gui_flask"
GATEWAY_PING_URL = "http://127.0.0.1:8090/ping"
GATEWAY_START_TIMEOUT_S = 20.0
GATEWAY_POLL_INTERVAL_S = 0.5
GUI_START_DELAY_S = 1.0
SHUTDOWN_TIMEOUT_S = 10.0


def resolve_venv_python() -> Path:
    if sys.platform.startswith("win"):
        python_path = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        python_path = ROOT / ".venv" / "bin" / "python"
    if not python_path.exists():
        raise FileNotFoundError(
            f"Virtual environment Python not found at {python_path}. "
            "Create the .venv first before running run_mykro.py."
        )
    return python_path


def start_process(python_path: Path, script_name: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(python_path), script_name],
        cwd=str(GUI_DIR),
    )


def wait_for_gateway_ready(proc: subprocess.Popen[bytes], timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(GATEWAY_PING_URL, timeout=2.0) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(GATEWAY_POLL_INTERVAL_S)
    return False


def stop_process(proc: subprocess.Popen[bytes] | None, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        print(f"Stopped {label} (pid {proc.pid}).")
        return
    except subprocess.TimeoutExpired:
        print(f"{label} did not stop in time; killing pid {proc.pid}.")
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Failed to kill {label} cleanly: {exc}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Error stopping {label}: {exc}")


def main() -> int:
    python_path = resolve_venv_python()
    gateway_proc: subprocess.Popen[bytes] | None = None
    gui_proc: subprocess.Popen[bytes] | None = None

    try:
        print(f"Using Python: {python_path}")
        print("Starting MYKRO gateway...")
        gateway_proc = start_process(python_path, "gateway.py")

        if not wait_for_gateway_ready(gateway_proc, GATEWAY_START_TIMEOUT_S):
            exit_code = gateway_proc.poll()
            print(
                "Gateway failed to become ready "
                f"within {GATEWAY_START_TIMEOUT_S:.0f}s"
                + (f" (exit code {exit_code})." if exit_code is not None else ".")
            )
            return exit_code if exit_code is not None else 1

        print("Gateway is ready. Starting desktop orchestrator...")
        # Circuit pre-check: warn on any tripped circuits before launching GUI
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8090/circuits", timeout=3.0
            ) as _cr:
                import json as _json
                _circuit_data = _json.loads(_cr.read().decode())
                _tripped = [
                    cid for cid, info in _circuit_data.get("circuits", {}).items()
                    if info.get("runtime_status") == "tripped"
                ]
                if _tripped:
                    print(f"WARNING: {len(_tripped)} circuit(s) are tripped: {', '.join(_tripped)}")
                    print("System may be degraded. Proceeding with GUI launch...")
                else:
                    _en = _circuit_data.get('enabled', 0)
                    _tot = _circuit_data.get('total', 0)
                    print(f"Circuits OK: {_en}/{_tot} enabled, 0 tripped.")
        except OSError as _ce:
            print(f"Circuit pre-check skipped: {_ce}")
        time.sleep(GUI_START_DELAY_S)
        gui_proc = start_process(python_path, "orchestrator_tk.py")

        assert gateway_proc is not None
        assert gui_proc is not None

        while True:
            gui_code = gui_proc.poll()
            gateway_code = gateway_proc.poll()

            if gui_code is not None:
                print(f"Desktop orchestrator exited with code {gui_code}.")
                return gui_code
            if gateway_code is not None:
                print(f"Gateway exited unexpectedly with code {gateway_code}.")
                return gateway_code

            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCtrl+C received. Shutting down MYKRO...")
        return 0
    finally:
        stop_process(gui_proc, "desktop orchestrator")
        stop_process(gateway_proc, "gateway")


if __name__ == "__main__":
    raise SystemExit(main())
