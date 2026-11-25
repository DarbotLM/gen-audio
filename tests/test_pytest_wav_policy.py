"""Keep the Python pytest policy covered while Python production code remains."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_require_wavs_turns_a_wav_skip_into_a_failure(tmp_path):
    shutil.copy2(REPO / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(
        "import pytest\n"
        "def test_needs_wav():\n"
        "    pytest.skip('x.wav is gitignored and not staged here')\n"
        "def test_other_skip():\n"
        "    pytest.skip('needs PowerShell')\n",
        encoding="utf-8",
    )

    def run(name: str, require: str | None) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if key != "GEN_AUDIO_REQUIRE_WAVS"}
        if require is not None:
            env["GEN_AUDIO_REQUIRE_WAVS"] = require
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "--basetemp",
                str(tmp_path / f"pytest-{name}"),
                str(tmp_path),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    plain = run("plain", None)
    assert plain.returncode == 0 and "2 skipped" in plain.stdout, plain.stdout
    strict = run("strict", "1")
    assert strict.returncode != 0 and "1 failed" in strict.stdout and "1 skipped" in strict.stdout, strict.stdout
    assert "forbids this skip: x.wav" in strict.stdout
