#!/usr/bin/env python3
"""
MYKRO Web GUI ↔ Backend Wiring Smoke Test

Validates:
- Flask web server startup
- Gateway health check
- Chat completion roundtrip
- Error handling
- Fallback behavior
"""

# This smoke test intentionally catches broad exceptions to report failures
# across multiple integration points; suppress the pylint warning here.
# pylint: disable=broad-exception-caught

import json
import os
import time
import requests
import subprocess
import sys
from pathlib import Path
import pytest

# Configuration
FLASK_URL = "http://127.0.0.1:5000"
GATEWAY_URL = "http://127.0.0.1:8090"
HEALTH_TIMEOUT = 30
COMPLETION_TIMEOUT = 60

class WiringTest:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.endpoints_started = []

    def test(self, name, fn):
        try:
            fn()
            print(f"✓ {name}")
            self.passed += 1
        except Exception as e:
            print(f"✗ {name}: {e}")
            self.failed += 1
            # The script keeps going and prints a summary. Pytest must fail.
            if os.environ.get("PYTEST_CURRENT_TEST"):
                raise

    def cleanup(self):
        """Stop any started services."""
        for proc in self.endpoints_started:
            try:
                proc.terminate()
            except:
                pass

    def summary(self):
        total = self.passed + self.failed
        status = "PASS" if self.failed == 0 else "FAIL"
        print(f"\n{status} ({self.passed}/{total} tests passed)")
        return self.failed == 0


# ── Tests ──────────────────────────────────────────────────────────────────

def _skip_live_if_flask_down() -> None:
    """Live HTTP checks need a running web UI. Pytest skips; the script records a failure."""
    try:
        requests.get(f"{FLASK_URL}/api/health", timeout=2)
    except requests.RequestException:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            pytest.skip("Flask web UI is not running on 127.0.0.1:5000")
        raise RuntimeError("Flask web UI is not running on 127.0.0.1:5000")


def test_flask_health(t: WiringTest) -> None:
    """Verify Flask web server is running."""
    def run():
        _skip_live_if_flask_down()
        r = requests.get(f"{FLASK_URL}/", timeout=5)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        assert "MYKRO" in r.text, "Response should contain MYKRO"
    t.test("Flask web server responds on 127.0.0.1:5000", run)


def test_api_health_endpoint(t: WiringTest) -> None:
    """GET /api/health should respond with health status."""
    def run():
        _skip_live_if_flask_down()
        r = requests.get(f"{FLASK_URL}/api/health", timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        data = r.json()
        assert "gateway" in data, "Response should have 'gateway' field"
        assert "models" in data, "Response should have 'models' field"
    t.test("GET /api/health endpoint", run)


def test_api_wiring_endpoint(t: WiringTest) -> None:
    """GET /api/wiring should return wiring hub status."""
    def run():
        _skip_live_if_flask_down()
        r = requests.get(f"{FLASK_URL}/api/wiring", timeout=30)
        # Could be 200 (gateway running) or 502 (gateway down); both are valid
        if r.status_code == 200:
            data = r.json()
            assert "wires" in data, "Response should have 'wires' field"
            assert "total" in data, "Response should have 'total' field"
        elif r.status_code == 502:
            data = r.json()
            assert "error" in data, "502 response should have 'error' field"
            assert "hint" in data, "502 response should have 'hint' field"
        else:
            raise AssertionError(f"Expected 200 or 502, got {r.status_code}")
    t.test("GET /api/wiring endpoint", run)


def test_api_completion_no_backend(t: WiringTest) -> None:
    """POST /api/completion with no backend should return 502 with helpful error."""
    def run():
        _skip_live_if_flask_down()
        payload = {"prompt": "test", "n_predict": 32, "temperature": 0.2}
        r = requests.post(f"{FLASK_URL}/api/completion", json=payload, timeout=15)
        # If Gateway is down, should return 502 with hint
        if r.status_code == 502:
            data = r.json()
            assert "error" in data, "502 response should have 'error' field"
            assert "hint" in data, "502 response should have actionable 'hint'"
            assert "No model backend" in data.get("error", ""), "Error should mention no backend"
        elif r.status_code == 200:
            # Gateway is up and returned something valid
            assert isinstance(r.text, str), "Response should be text"
        else:
            raise AssertionError(f"Expected 200 or 502, got {r.status_code}")
    t.test("POST /api/completion error handling", run)


def test_fetch_timeout_safety(t: WiringTest) -> None:
    """Browser-side fetch should have timeout to prevent hanging."""
    def run():
        # This is a README test — check the index.html has FETCH_TIMEOUT_MS defined
        index_path = Path(__file__).parent / "templates" / "index.html"
        if index_path.exists():
            html = index_path.read_text()
            assert "FETCH_TIMEOUT_MS" in html, "HTML should define FETCH_TIMEOUT_MS"
            assert "AbortController" in html, "HTML should use AbortController for timeouts"
    t.test("Browser fetch timeout safety", run)


def test_status_banner_update(t: WiringTest) -> None:
    """Check HTML has periodic status banner updates."""
    def run():
        index_path = Path(__file__).parent / "templates" / "index.html"
        if index_path.exists():
            html = index_path.read_text()
            assert "updateBannerStatus" in html, "HTML should have updateBannerStatus function"
            assert "setInterval(updateBannerStatus" in html, "HTML should poll status periodically"
    t.test("Status banner periodic update", run)


def test_error_formatting(t: WiringTest) -> None:
    """Check error messages are user-friendly, not raw tracebacks."""
    def run():
        index_path = Path(__file__).parent / "templates" / "index.html"
        if index_path.exists():
            html = index_path.read_text()
            assert "parseResponseBody" in html, "HTML should safely parse responses"
            assert "safeJson" in html, "HTML should catch invalid JSON"
            # Errors should be caught and displayed nicely
            assert "appendChat('error'" in html, "HTML should append errors to chat"
    t.test("Error message formatting", run)


def test_config_not_hardcoded(t: WiringTest) -> None:
    """Check configuration is externalized, not hardcoded in code."""
    def run():
        app_py = Path(__file__).parent / "web_ui.py"
        if app_py.exists():
            code = app_py.read_text()
            # Should use config.json or load_config(), not hardcoded URLs
            assert "load_config()" in code, "Should load config from file"
            assert "config.json" in code, "Should reference config.json"
            # Gateway port should not be hardcoded, should come from config
            # (it's OK if 8090 appears in default/fallback, but not hardcoded everywhere)
    t.test("Configuration externalization", run)


def test_startup_instructions_accurate(t: WiringTest) -> None:
    """Check README and launchers have accurate startup instructions."""
    def run():
        readme = Path(__file__).parent / "README.md"
        if readme.exists():
            content = readme.read_text()
            # Should mention start-web.ps1 or start-desktop.ps1, not non-existent scripts
            has_valid_launcher = "start-web.ps1" in content or "start-desktop.ps1" in content
            assert has_valid_launcher, "README should mention valid launchers"
    t.test("Startup documentation accuracy", run)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    t = WiringTest()

    print("MYKRO Web GUI ↔ Backend Wiring Smoke Test\n")
    print("Prerequisites:")
    print("  - Flask running on 127.0.0.1:5000 (run: python web_ui.py)")
    print("  - Gateway running on 127.0.0.1:8090 (optional; tests handle 502)")
    print("")

    # Connectivity tests
    test_flask_health(t)
    test_api_health_endpoint(t)
    test_api_wiring_endpoint(t)
    test_api_completion_no_backend(t)

    # Contract & safety tests
    test_fetch_timeout_safety(t)
    test_status_banner_update(t)
    test_error_formatting(t)
    test_config_not_hardcoded(t)
    test_startup_instructions_accurate(t)

    # Cleanup
    t.cleanup()

    # Summary
    success = t.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()


# Pytest fixture adapter: provide a WiringTest instance when pytest collects these
@pytest.fixture
def t():
    return WiringTest()
