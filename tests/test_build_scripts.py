"""Check logic of scripts/build-tauri-windows.ps1, through scripts/lib/devenv.ps1.

The build script itself only runs on Windows. The checks it relies on live
in devenv.ps1, which is dot-sourced here in PowerShell (pwsh or Windows
PowerShell; skips without one).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEVENV = REPO / "scripts" / "lib" / "devenv.ps1"
BUILD = REPO / "scripts" / "build-tauri-windows.ps1"
PACKER = REPO / "scripts" / "pack-windows-msix-nuget.ps1"
SHELL = shutil.which("pwsh") or shutil.which("powershell")
needs_shell = pytest.mark.skipif(SHELL is None, reason="needs PowerShell")


def _ps(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    script = f". '{DEVENV}'; $ErrorActionPreference = 'Stop'; {command}"
    return subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-Command", script], cwd=cwd, capture_output=True, text=True,
                          timeout=600)


def _library(tmp_path: Path, files: dict[str, bytes], lock: dict[str, bytes]) -> Path:
    root = tmp_path / "root"
    library = root / "apps" / "desktop" / "public" / "library"
    library.mkdir(parents=True)
    (root / "schemas" / "asset-object").mkdir(parents=True)
    for name, data in files.items():
        (library / name).write_bytes(data)
    media = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in lock.items()}
    (root / "schemas" / "asset-object" / "media.lock.json").write_text(json.dumps({"media": media}), encoding="utf-8")
    return root


# --- C1: no release build without the locked WAVs ----------------------------

@needs_shell
def test_wav_check_names_missing_short_and_edited_wavs_and_is_silent_when_all_match(tmp_path):
    good, other = b"RIFF good", b"RIFF other"
    root = _library(tmp_path, {"a.wav": good, "c.wav": b"RIFF goox", "d.wav": good + b"x"},
                    {"a.wav": good, "b.wav": other, "c.wav": good, "d.wav": good})
    out = _ps(f"Get-LibraryWavProblem -Root '{root}'", root)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert any(line.startswith("WAV_MISSING b.wav") for line in lines), lines
    assert any(line.startswith("WAV_MISMATCH c.wav") for line in lines), lines  # same length, other bytes
    assert any(line.startswith("WAV_BYTES d.wav") for line in lines), lines
    assert not any(" a.wav" in line for line in lines), lines
    ok = _library(tmp_path / "ok", {"a.wav": good}, {"a.wav": good})
    clean = _ps(f"Get-LibraryWavProblem -Root '{ok}'", ok)
    assert clean.returncode == 0 and clean.stdout.strip() == "", clean.stdout + clean.stderr


@needs_shell
def test_wav_check_fails_on_an_empty_or_missing_lock_instead_of_passing(tmp_path):
    empty = _library(tmp_path, {}, {})
    assert _ps(f"Get-LibraryWavProblem -Root '{empty}'", empty).returncode != 0
    nolock = tmp_path / "nolock"
    nolock.mkdir()
    assert _ps(f"Get-LibraryWavProblem -Root '{nolock}'", nolock).returncode != 0



@needs_shell
def test_import_library_wav_stages_only_locked_bytes_from_another_copy(tmp_path):
    """DD-8: with SMAX offline the build could only refuse. -WavSource stages the
    locked WAVs from another verified copy (a node or share); a source file with
    other bytes is reported and not copied, so the gate after it still refuses."""
    good, other = b"RIFF good", b"RIFF other"
    root = _library(tmp_path, {}, {"a.wav": good, "b.wav": other, "c.wav": good})
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.wav").write_bytes(good)
    (source / "b.wav").write_bytes(other + b"!")
    out = _ps(f"Import-LibraryWav -Root '{root}' -From '{source}'", root)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert any(line.startswith("WAV_STAGED a.wav") for line in lines), lines
    assert any(line.startswith("WAV_SOURCE_MISMATCH b.wav") for line in lines), lines
    assert any(line.startswith("WAV_SOURCE_MISSING c.wav") for line in lines), lines
    library = root / "apps" / "desktop" / "public" / "library"
    assert (library / "a.wav").read_bytes() == good
    assert not (library / "b.wav").exists() and not (library / "c.wav").exists()
    gate = _ps(f"Get-LibraryWavProblem -Root '{root}'", root).stdout
    assert "WAV_MISSING b.wav" in gate and "WAV_MISSING c.wav" in gate and " a.wav" not in gate
    assert _ps(f"Import-LibraryWav -Root '{root}' -From '{tmp_path / 'nowhere'}'", root).returncode != 0


def test_build_refusal_names_the_way_to_stage_the_wavs():
    text = BUILD.read_text(encoding="utf-8")
    assert "[string]$WavSource" in text
    gate = text.index("Get-LibraryWavProblem -Root $root")
    assert text.index("Import-LibraryWav -Root $root -From $WavSource") < gate
    after = text[gate:text.index("Write-Step 'library WAVs match", gate)]
    assert "-WavSource" in after and "throw" in after

def test_build_script_checks_the_wavs_before_building_and_never_skips():
    text = BUILD.read_text(encoding="utf-8")
    gate = text.index("Get-LibraryWavProblem -Root $root")
    assert gate < text.index("Invoke-CargoBinBuild -Package") and gate < text.index("npx $($tauriArgs")
    after = text[gate:text.index("Write-Step 'library WAVs match", gate)]
    assert "throw" in after and "skip" not in after.lower()


# --- C2: sidecar freshness is cargo's fingerprint + content, never mtime ---------

def _crate(tmp_path: Path) -> Path:
    crate = tmp_path / "probe"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n\n[[bin]]\nname = "probe"\n'
                                      'path = "src/main.rs"\n\n[workspace]\n', encoding="utf-8")
    (crate / "src" / "main.rs").write_text('fn main() { println!("v1"); }\n', encoding="utf-8")
    return crate


def _build(crate: Path) -> dict:
    env_target = crate / "target"
    out = _ps(f"$env:CARGO_TARGET_DIR = '{env_target}'; Invoke-CargoBinBuild -Package probe -Bin probe | ConvertTo-Json -Compress", crate)
    assert out.returncode == 0, out.stdout + out.stderr
    assert " is stale" not in out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_shell
@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")
def test_noop_rebuild_is_not_stale_even_when_the_binary_mtime_is_old(tmp_path):
    import os

    crate = _crate(tmp_path)
    first = _build(crate)
    assert first["Fresh"] is False and Path(first["Executable"]).is_file()
    exe = Path(first["Executable"])
    before = hashlib.sha256(exe.read_bytes()).hexdigest()
    os.utime(exe, (978307200, 978307200))  # 2001-01-01: far older than any run start (the SMAX case)
    second = _build(crate)
    assert second == {"Executable": first["Executable"], "Fresh": True}, "a no-op rebuild must not be stale or rebuilt"
    assert hashlib.sha256(exe.read_bytes()).hexdigest() == before
    (crate / "src" / "main.rs").write_text('fn main() { println!("v2"); }\n', encoding="utf-8")
    third = _build(crate)
    assert third["Fresh"] is False and hashlib.sha256(exe.read_bytes()).hexdigest() != before, "a source change rebuilds"


@needs_shell
def test_select_cargo_bin_artifact_reads_cargo_json_and_fails_without_the_bin(tmp_path):
    lines = [
        json.dumps({"reason": "compiler-artifact", "target": {"name": "gen_audio_core", "kind": ["lib"]}, "executable": None, "fresh": True}),
        json.dumps({"reason": "compiler-artifact", "target": {"name": "gen-audio-mcp", "kind": ["bin"]},
                    "executable": "C:/t/release/gen-audio-mcp.exe", "fresh": True}),
        json.dumps({"reason": "build-finished", "success": True}),
    ]
    feed = tmp_path / "lines.json"
    feed.write_text(json.dumps(lines), encoding="utf-8")
    load = f"$l = [string[]](Get-Content -LiteralPath '{feed}' -Raw | ConvertFrom-Json)"
    out = _ps(f"{load}; Select-CargoBinArtifact -Line $l -Bin gen-audio-mcp | ConvertTo-Json -Compress", tmp_path)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"Executable": "C:/t/release/gen-audio-mcp.exe", "Fresh": True}
    missing = _ps(f"{load}; Select-CargoBinArtifact -Line $l -Bin other", tmp_path)
    assert missing.returncode != 0 and "no compiler-artifact for bin other" in missing.stdout + missing.stderr


def test_build_script_judges_the_sidecar_by_cargo_and_hash_not_mtime():
    text = BUILD.read_text(encoding="utf-8")
    block = text[text.index("Write-Step 'build gen-audio-mcp sidecar'"):text.index("Write-Step 'frontend dependencies")]
    assert "Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp" in block
    assert "Assert-Fresh" not in block and "LastWriteTime" not in block
    assert "$stagedHash -ne $sidecarHash" in block


def test_build_uses_the_local_tauri_cli_and_hash_checks_both_external_binaries():
    text = BUILD.read_text(encoding="utf-8")
    assert "$TauriCli = 'tau" + "ri'" in text
    assert "$tauriArgs = @('--no', '--', $TauriCli, 'bui" + "ld')" in text
    block = text[text.index("Write-Step 'build gen-audio-mcp sidecar'"):
                 text.index("Write-Step 'frontend dependencies")]
    assert "Invoke-CargoBinBuild -Package gen-audio-mcp -Bin gen-audio-mcp" in block
    assert "Invoke-CargoBinBuild -Package gen-audio-acp -Bin gen-audio-acp" in block
    assert "acpInit.result.protocolVersion -ne 1" in block
    assert "acpInit.result.agentInfo.name -ne 'gen-audio'" in block
    assert "staged sidecar $staged" in block and "staged sidecar $stagedAcp" in block
    assert "$stagedHash -ne $sidecarHash" in block
    assert "$stagedAcpHash -ne $acpHash" in block

    config = json.loads((REPO / "apps/desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["externalBin"] == [
        "binaries/gen-audio-mcp",
        "binaries/gen-audio-acp",
    ]
    build_rs = (REPO / "apps/desktop/src-tauri/build.rs").read_text(encoding="utf-8")
    debug_config = build_rs.index("std::env::set_var")
    debug_block = build_rs[debug_config:build_rs.index("} else {", debug_config)]
    assert '"bundle":{"externalBin":["binaries/gen-audio-mcp"]}' in debug_block


def test_msix_and_nuget_pack_after_existing_installer_provenance_checks():
    build = BUILD.read_text(encoding="utf-8")
    pack = PACKER.read_text(encoding="utf-8")
    installer_payload = build.index("INSTALLER_PAYLOAD kind=$kind")
    package_call = build.index("pack-windows-msix-nuget.ps1", installer_payload)
    assert installer_payload < package_call
    assert "ExpectedExeSha256 = $exe.Sha256" in build
    assert "ExpectedMcpSha256 = $sidecarHash" in build
    assert "ExpectedAcpSha256 = $acpHash" in build
    assert "if ($CertificateThumbprint)" in build

    for name in ("gen-audio.exe", "gen-audio-mcp.exe", "gen-audio-acp.exe"):
        assert name in pack
    assert "Assert-ArchivePayload -Kind 'msix'" in pack
    assert "Assert-ArchivePayload -Kind 'nupkg'" in pack
    assert "signed = if ($certificate) { 'yes' } else { 'no' }" in pack
    assert "if ($certificate)" in pack and "signtool.exe" in pack
    assert "CertificateThumbprint is not in CurrentUser" in pack
    assert "scripts/pack-windows-msix-nuget.ps1" in (
        REPO / "scripts/test.ps1"
    ).read_text(encoding="utf-8")


def test_desktop_dependency_pins_keep_vite_6_and_public_registry():
    package = json.loads((REPO / "apps/desktop/package.json").read_text(encoding="utf-8"))
    lock = json.loads((REPO / "apps/desktop/package-lock.json").read_text(encoding="utf-8"))
    cli_package = "@tauri-apps/" + "cli"
    assert package["engines"]["node"] == ">=26.0.0 <27"
    assert package["dependencies"]["@tauri-apps/api"] == "2.12.5"
    assert package["devDependencies"][cli_package] == "2.12.6"
    assert package["devDependencies"]["vite"] == "^6.4.5"
    assert lock["packages"]["node_modules/@tauri-apps/api"]["version"] == "2.12.5"
    assert lock["packages"][f"node_modules/{cli_package}"]["version"] == "2.12.6"
    assert lock["packages"]["node_modules/vite"]["version"] == "6.4.5"
    resolved = [
        value["resolved"]
        for value in lock["packages"].values()
        if isinstance(value, dict) and "resolved" in value
    ]
    assert resolved and all(url.startswith("https://registry.npmjs.org/") for url in resolved)
    npmrc = (REPO / "apps/desktop/.npmrc").read_text(encoding="utf-8")
    assert "registry=https://registry.npmjs.org/" in npmrc


# --- B4: every build run leaves its full log in target/logs -------------------

@needs_shell
@pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git; on Windows this would start a real build")
def test_build_script_saves_its_transcript_even_when_it_throws(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(BUILD, root / "scripts" / BUILD.name)
    shutil.copy2(DEVENV, root / "scripts" / "lib" / DEVENV.name)
    shutil.copy2(REPO / "scripts" / "runtime.json", root / "scripts" / "runtime.json")
    shutil.copy2(REPO / "scripts" / "lib" / "runtime-config.ps1", root / "scripts" / "lib" / "runtime-config.ps1")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], cwd=root, check=True)
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    # Off Windows the script throws right after it starts; the log must still be written and closed.
    run = subprocess.run([SHELL, "-NoProfile", "-File", str(root / "scripts" / BUILD.name), "-Mode", "NoBundle"], cwd=root,
                         capture_output=True, text=True, timeout=120)
    out = run.stdout + run.stderr
    assert run.returncode != 0 and "run it on Windows" in out, out
    logs = sorted((root / "target" / "logs").glob(f"build-{sha}-*.log"))
    assert len(logs) == 1, (out, list((root / "target").rglob("*")) if (root / "target").exists() else "no target/")
    assert re.fullmatch(rf"build-{sha}-\d{{8}}-\d{{6}}\.log", logs[0].name), logs[0].name
    text = logs[0].read_text(encoding="utf-8-sig")
    assert "build-tauri-windows.ps1 -Mode NoBundle" in text and "run it on Windows" in text
    assert "PowerShell transcript end" in text, "Stop-Transcript runs in finally, even on a throw"
    assert f"BUILD_LOG {logs[0]}" in out or f"BUILD_LOG {logs[0].as_posix()}" in out, out


def test_build_ok_names_the_log_and_the_transcript_stops_in_finally():
    text = BUILD.read_text(encoding="utf-8")
    assert re.search(r'Write-Output "BUILD_OK [^"]*log=\$buildLog', text), "BUILD_OK carries the log path"
    start = text.index("Start-Transcript")
    body = text[start:]
    assert re.search(r"\}\s*finally\s*\{\s*Stop-Transcript", body), "the transcript stops in a finally block"
    assert text.index("Write-Step \"build-tauri-windows.ps1 -Mode") > start, "the whole run is inside the transcript"


# --- Low: dist, exe and installers are judged by content, never mtime ---------

@needs_shell
def test_a_noop_rebuild_output_is_unchanged_not_stale_and_a_missing_one_fails(tmp_path):
    import os

    out = tmp_path / "gen-audio.exe"
    out.write_bytes(b"MZ v1")
    snap = "$b = Get-OutputFingerprint -LiteralPath '{p}'"
    noop = _ps(f"{snap.format(p=out)}; [IO.File]::SetLastWriteTime('{out}', [datetime]'2001-01-01'); "
               f"Assert-BuildOutput -LiteralPath '{out}' -What exe -Before $b | ConvertTo-Json -Compress", tmp_path)
    assert noop.returncode == 0 and "stale" not in noop.stdout + noop.stderr, noop.stdout + noop.stderr
    result = json.loads(noop.stdout.strip().splitlines()[-1])
    assert result["State"] == "unchanged" and result["Sha256"] == hashlib.sha256(b"MZ v1").hexdigest()
    assert os.path.getmtime(out) < 1e9, "the output really is older than any run start"
    changed = _ps(f"{snap.format(p=out)}; [IO.File]::WriteAllText('{out}', 'MZ v2'); "
                  f"(Assert-BuildOutput -LiteralPath '{out}' -What exe -Before $b).State", tmp_path)
    assert changed.stdout.strip() == "rebuilt", changed.stdout + changed.stderr
    new = tmp_path / "new.msi"
    created = _ps(f"{snap.format(p=new)}; [IO.File]::WriteAllText('{new}', 'msi'); "
                  f"(Assert-BuildOutput -LiteralPath '{new}' -What installer -Before $b).State", tmp_path)
    assert created.stdout.strip() == "rebuilt", created.stdout + created.stderr
    missing = _ps(f"Assert-BuildOutput -LiteralPath '{tmp_path / 'gone.exe'}' -What exe -Before $null", tmp_path)
    assert missing.returncode != 0 and "exe is missing after the build" in missing.stdout + missing.stderr


@needs_shell
def test_dist_copy_drift_names_missing_and_changed_files_and_is_silent_when_equal(tmp_path):
    public, dist = tmp_path / "public", tmp_path / "dist"
    for root in (public, dist):
        (root / "library").mkdir(parents=True)
        (root / "library" / "assets.json").write_text("{}", encoding="utf-8")
    clean = _ps(f"Get-DistCopyDrift -Source '{public}' -Dist '{dist}'", tmp_path)
    assert clean.returncode == 0 and clean.stdout.strip() == "", clean.stdout + clean.stderr
    (public / "library" / "assets.json").write_text('{"new": 1}', encoding="utf-8")
    (public / "library" / "a.wav").write_bytes(b"RIFF")
    drift = _ps(f"Get-DistCopyDrift -Source '{public}' -Dist '{dist}'", tmp_path)
    lines = sorted(drift.stdout.strip().splitlines())
    assert lines == ["DIST_CHANGED library/assets.json", "DIST_MISSING library/a.wav"], drift.stdout + drift.stderr


def test_build_script_has_no_mtime_freshness_left():
    text = BUILD.read_text(encoding="utf-8")
    for old in ("Assert-Fresh", "LastWriteTime", "$runStart"):
        assert old not in text, f"{old} is an mtime check"
    assert "$indexPath = Join-Path $desktop 'dist\\index.html'" in text and "$exePath = Join-Path $releaseDir 'gen-audio.exe'" in text
    for var in ("$indexPath", "$exePath", "$installer"):
        assert f"Assert-BuildOutput -LiteralPath {var} " in text, var
    assert "Get-DistCopyDrift -Source" in text
    assert "$conf.version" in text, "installers are picked by this build's version, not by newest mtime"
