import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROWSER_TEST = ROOT / "tests/browser_e2e.js"
RUNTIME_NODE = os.getenv("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
RUNTIME_MODULES = os.getenv("CODEX_PRIMARY_RUNTIME_NODE_MODULES")


def _playwright_available() -> bool:
    if not RUNTIME_NODE or not RUNTIME_MODULES:
        return False
    result = subprocess.run(
        [
            RUNTIME_NODE,
            "-e",
            (
                "const fs=require('fs'); const p=require('playwright'); "
                "process.exit(fs.existsSync(p.chromium.executablePath()) && "
                "fs.existsSync(p.webkit.executablePath()) ? 0 : 1)"
            ),
        ],
        env={**os.environ, "NODE_PATH": RUNTIME_MODULES},
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize("scenario", ["flow", "pagination"])
@pytest.mark.skipif(
    not _playwright_available(),
    reason="Playwright browsers are unavailable in this environment",
)
def test_real_browser_reading_flow(scenario):
    result = subprocess.run(
        [RUNTIME_NODE, str(BROWSER_TEST), scenario],
        cwd=ROOT,
        env={**os.environ, "NODE_PATH": RUNTIME_MODULES},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"{scenario}: ok" in result.stdout
