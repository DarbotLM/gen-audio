"""The deployment entry point resolves paths from its checkout, not its caller."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SHELL = shutil.which("pwsh") or shutil.which("powershell")
REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(SHELL is None, reason="needs PowerShell")
@pytest.mark.parametrize("service,port", [("Mcp", 8765), ("Node", 8002), ("Desktop", 8765), ("Preview", 1420)])
def test_launch_plan_is_checkout_relative(tmp_path, service, port):
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "run.ps1", scripts / "run.ps1")
    shutil.copy2(REPO / "scripts" / "runtime.json", scripts / "runtime.json")
    shutil.copy2(REPO / "scripts" / "lib" / "runtime-config.ps1", scripts / "lib" / "runtime-config.ps1")
    result = subprocess.run(
        [SHELL, "-NoProfile", "-File", str(scripts / "run.ps1"), "-Service", service, "-Check"],
        cwd=tmp_path,
        env={**os.environ, "GENAID_MODEL_BASE": str(tmp_path / "stale-models")},
        capture_output=True, text=True, check=True, timeout=30,
    )
    plan = json.loads(result.stdout)
    env = plan["environment"]
    assert Path(env["GEN_AUDIO_REPO"]) == repo
    assert Path(env["GENAID_MODEL_BASE"]) == repo / "models"
    assert env["GENAID_MODEL_BASE"] == env["GEN_AUDIO_MODEL_DIR"]
    assert Path(env["GEN_AUDIO_WORK_DIR"]) == repo / "artifacts" / "runtime" / "mcp"
    assert env["GEN_AUDIO_MCP_ADDR"] == f"127.0.0.1:{port if service in ('Mcp', 'Desktop') else 8765}"
    assert not (repo / "artifacts").exists(), "-Check must not mutate the checkout"
    if service == "Desktop":
        assert Path(plan["executable"]).name == "gen-audio.exe"
    elif service == "Preview":
        assert "--strictPort" in plan["arguments"]


@pytest.mark.skipif(SHELL is None, reason="needs PowerShell")
def test_launch_plan_reads_checkout_descriptor_ports_without_user_env(tmp_path):
    repo = tmp_path / "checkout"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "run.ps1", scripts / "run.ps1")
    shutil.copy2(REPO / "scripts" / "runtime.json", scripts / "runtime.json")
    shutil.copy2(REPO / "scripts" / "lib" / "runtime-config.ps1", scripts / "lib" / "runtime-config.ps1")
    runtime = repo / "artifacts" / "runtime"
    runtime.mkdir(parents=True)
    descriptor = {
        "checkout": f"  {str(repo).lower()}\\  ",
        "ports": {"Node": 18002, "Mcp": 18765, "Desktop": 18765, "Preview": 11420},
    }
    (runtime / "node-environment.json").write_text(json.dumps(descriptor), encoding="utf-8")
    result = subprocess.run(
        [SHELL, "-NoProfile", "-File", str(scripts / "run.ps1"), "-Service", "Node", "-Check"],
        cwd=tmp_path,
        env={**os.environ, "GEN_AUDIO_NODE_PORT": "9999"},
        capture_output=True, text=True, check=True, timeout=30,
    )
    plan = json.loads(result.stdout)
    assert "port=18002" in plan["arguments"][-1]
    assert plan["environment"]["GEN_AUDIO_NODE_PORT"] == "18002"
    assert plan["environment"]["GEN_AUDIO_MCP_ADDR"] == "127.0.0.1:18765"


@pytest.mark.skipif(SHELL is None or os.name != "nt", reason="needs Windows named mutex support")
def test_toolbox_lease_uses_canonical_trimmed_checkout_before_mutation(tmp_path):
    canonical = str(REPO.resolve()).rstrip("\\/")
    digest = hashlib.sha256(canonical.upper().encode("utf-8")).hexdigest()[:16]
    mutex = rf'Local\gen-audio-toolbox-{digest}'
    holder_code = (
        f"$m=New-Object Threading.Mutex($false, '{mutex}'); "
        "if(-not $m.WaitOne(0)){exit 9}; 'READY'; [Console]::In.ReadLine() | Out-Null; "
        "$m.ReleaseMutex(); $m.Dispose()"
    )
    holder = subprocess.Popen(
        [SHELL, "-NoProfile", "-NonInteractive", "-Command", holder_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout and holder.stdout.readline().strip() == "READY"
        padded = f'  {canonical.lower()}\\  '
        result = subprocess.run(
            [SHELL, "-NoProfile", "-File", str(REPO / "scripts" / "toolbox.ps1"),
             "-Step", "Test", "-SkipTests", "-RepoRoot", padded,
             "-LogRoot", str(tmp_path / "logs"), "-NoBrowser"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert "Another toolbox run owns the checkout lease" in output, output
        assert not (tmp_path / "logs").exists(), "logging/mutation must not happen before lease acquisition"
    finally:
        if holder.stdin:
            holder.stdin.write("release\n")
            holder.stdin.flush()
        holder.wait(timeout=30)


def test_toolbox_cleanup_terminates_process_tree_and_verifies_port_release():
    text = (REPO / "scripts" / "toolbox.ps1").read_text(encoding="utf-8")
    service_block = text[text.index("function Invoke-NodeService"):text.index("function Invoke-ServiceSuite")]
    assert "Complete-ToolboxProcessTree -RootProcessId $existing.Id" in service_block
    assert "Complete-ToolboxProcessTree -RootProcessId $process.Id" in service_block
    assert "-AdditionalProcessIds @($trackedChildIds)" in service_block
    assert service_block.count("Wait-ToolboxPortReleased -Port $Port") >= 2
    tree_block = text[text.index("function Get-ToolboxProcessTreeId"):text.index("function Invoke-NodeService")]
    assert "ParentProcessId" in tree_block and "Sort-Object { $depths[$_] } -Descending" in tree_block
    assert "ExclusiveAddressUse = $true" in tree_block
    assert service_block.count("Stop-Process -Id $process.Id") == 0


@pytest.mark.skipif(SHELL is None, reason="needs PowerShell")
def test_native_build_entry_point_parses():
    path = str(REPO / "scripts" / "build-tauri-windows.ps1").replace("'", "''")
    result = subprocess.run(
        [SHELL, "-NoProfile", "-Command",
         f"$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}',"
         "[ref]$null,[ref]$e); if($e.Count){$e | Out-String | Write-Output; exit 1}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
