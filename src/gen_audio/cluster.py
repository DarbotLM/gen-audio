"""Colibri-inspired Windows cluster scheduling for resident speech workers.

The coordinator uses SSH as its authenticated control transport. Worker HTTP
ports stay on each node's loopback interface; the coordinator never exposes a
model or credentials on a public LAN port.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

_NODE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SSH_TARGET = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
_PORTS = {"vibevoice": 8996, "personaplex": 8997}
_REQUIRED_WORKER_GIB = {"vibevoice": 26, "personaplex": 18}


@dataclass(frozen=True)
class ClusterNode:
    node_id: str
    ssh: str
    root: str
    output_root: str
    voice_root: str
    model_dirs: dict[str, str]
    weight: float = 1.0

    @classmethod
    def parse(cls, row: dict) -> "ClusterNode":
        if not isinstance(row, dict):
            raise ValueError("cluster node must be an object")
        node_id = row.get("id")
        target = row.get("ssh")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            raise ValueError("cluster node id must be a lowercase slug")
        if not isinstance(target, str) or not _SSH_TARGET.fullmatch(target):
            raise ValueError(f"cluster node {node_id} ssh must be user@host")
        for name in ("root", "output_root", "voice_root"):
            if not isinstance(row.get(name), str) or not row[name].strip():
                raise ValueError(f"cluster node {node_id} needs {name}")
        models = row.get("model_dirs")
        if not isinstance(models, dict) or set(models) != {"vibevoice", "personaplex"}:
            raise ValueError(f"cluster node {node_id} must name both model_dirs")
        weight = float(row.get("weight", 1))
        if weight <= 0:
            raise ValueError(f"cluster node {node_id} weight must be positive")
        return cls(
            node_id=node_id,
            ssh=target,
            root=row["root"],
            output_root=row["output_root"],
            voice_root=row["voice_root"],
            model_dirs={key: str(value) for key, value in models.items()},
            weight=weight,
        )


def load_nodes(path: str | Path | None = None) -> tuple[ClusterNode, ...]:
    raw = (
        Path(path)
        if path is not None
        else Path(os.environ.get("GEN_AUDIO_CLUSTER_CONFIG", "runtimes/cluster.windows.json"))
    )
    payload = json.loads(raw.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("cluster config schema_version must be 1.0")
    rows = payload.get("nodes")
    if not isinstance(rows, list) or not rows:
        raise ValueError("cluster config needs at least one node")
    nodes = tuple(ClusterNode.parse(row) for row in rows)
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ValueError("cluster node ids must be unique")
    return nodes


def _encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ssh(node: ClusterNode, script: str, *, timeout: float) -> dict:
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"ConnectTimeout={max(5, min(30, int(timeout)))}",
        node.ssh,
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        _encoded_powershell(script),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{node.node_id} SSH failed ({result.returncode}): "
            f"{result.stderr.strip()[-1200:] or result.stdout.strip()[-1200:]}"
        )
    output = result.stdout
    if len(output) > 4 * 1024 * 1024:
        raise RuntimeError(f"{node.node_id} returned an oversized cluster receipt")
    value = None
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            value = parsed
            break
    if value is None:
        raise RuntimeError(f"{node.node_id} returned no JSON from the worker probe")
    if not isinstance(value, dict):
        raise ValueError(f"{node.node_id} returned a non-object cluster receipt")
    return value


def probe_node(node: ClusterNode, *, timeout: float = 30) -> dict:
    root = node.root.replace("'", "''")
    output_root = node.output_root.replace("'", "''")
    voice_root = node.voice_root.replace("'", "''")
    vibe_model = node.model_dirs["vibevoice"].replace("'", "''")
    persona_model = node.model_dirs["personaplex"].replace("'", "''")
    script = f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$cpu = @(Get-CimInstance Win32_Processor)
$gpu = @()
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {{
  foreach ($line in @(& nvidia-smi --query-gpu=index,name,memory.total,memory.free,memory.used,driver_version --format=csv,noheader 2>$null)) {{
    $parts = $line -split ',\\s*'
    if ($parts.Count -ge 6) {{
      $gpu += [pscustomobject]@{{index=[int]$parts[0];name=$parts[1];total_mib=[int]($parts[2] -replace '\\D','');free_mib=[int]($parts[3] -replace '\\D','');used_mib=[int]($parts[4] -replace '\\D','');driver=$parts[5]}}
    }}
  }}
}}
$workers = @{{}}
foreach ($entry in @{{vibevoice=8996;personaplex=8997}}.GetEnumerator()) {{
  try {{
    $workers[$entry.Key] = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $entry.Value + '/v1/status') -TimeoutSec 2
  }} catch {{
    $workers[$entry.Key] = [pscustomobject]@{{loaded=$false;phase='offline'}}
  }}
}}
$outputWritable = $false
$probe = Join-Path '{output_root}' ('.gen-audio-probe-' + [guid]::NewGuid().ToString('N'))
try {{
  New-Item -ItemType Directory -Path '{output_root}' -Force | Out-Null
  'probe' | Set-Content -LiteralPath $probe -Encoding ascii -NoNewline
  $outputWritable = $true
}} catch {{
  $outputWritable = $false
}} finally {{
  Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
}}
$receipt = [pscustomobject]@{{
  ok=$true
  node_id='{node.node_id}'
  host=$env:COMPUTERNAME
  os=$os.Caption
  build=$os.BuildNumber
  cpu_cores=($cpu | Measure-Object NumberOfLogicalProcessors -Sum).Sum
  ram_total_bytes=[long]$os.TotalVisibleMemorySize * 1024
  ram_free_bytes=[long]$os.FreePhysicalMemory * 1024
  gpu=$gpu
  root='{root}'
  root_exists=Test-Path -LiteralPath '{root}'
  output_root='{output_root}'
  output_writable=$outputWritable
  voice_root='{voice_root}'
  voice_root_exists=Test-Path -LiteralPath '{voice_root}'
  weight={node.weight!r}
  tools=[pscustomobject]@{{
    python=(Get-Command python.exe,py.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    dotnet=(Get-Command dotnet.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    git=(Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
  }}
  model_paths=[pscustomobject]@{{
    vibevoice='{vibe_model}'
    vibevoice_exists=Test-Path -LiteralPath (Join-Path '{vibe_model}' 'config.json')
    personaplex='{persona_model}'
    personaplex_exists=Test-Path -LiteralPath (Join-Path '{persona_model}' 'model.safetensors')
  }}
  workers=$workers
}}
$receipt | ConvertTo-Json -Compress -Depth 12
"""
    return _ssh(node, script, timeout=timeout)


def probe(nodes: tuple[ClusterNode, ...]) -> dict:
    receipts = []
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        futures = {pool.submit(probe_node, node): node for node in nodes}
        for future in as_completed(futures):
            node = futures[future]
            try:
                receipt = future.result()
            except Exception as exc:
                receipt = {
                    "ok": False,
                    "node_id": node.node_id,
                    "errors": [{"code": "probe_failed", "message": str(exc)}],
                }
            receipts.append(receipt)
    receipts.sort(key=lambda item: item["node_id"])
    return {"schema_version": "1.0", "nodes": receipts}


def _score(node: dict, engine: str) -> float:
    if not node.get("ok"):
        return float("-inf")
    models = node.get("model_paths") or {}
    workers = node.get("workers") or {}
    if not models.get(f"{engine}_exists"):
        return float("-inf")
    required = _REQUIRED_WORKER_GIB[engine]
    if float(node.get("ram_free_bytes", 0)) / 1024**3 < required:
        return float("-inf")
    worker = workers.get(engine) or {}
    score = 1.0 + (10.0 if worker.get("loaded") else 0.0)
    gpus = node.get("gpu") or []
    if gpus:
        available = max(float(gpu.get("free_mib", 0)) for gpu in gpus)
        total = max(float(gpu.get("total_mib", 0)) for gpu in gpus)
        score += min(16.0, total / 1024.0) + min(8.0, available / 1024.0)
    else:
        score += float(node.get("cpu_cores", 0)) / 12.0
    score += float(node.get("ram_free_bytes", 0)) / 1024**3 / 16.0
    return score * float(node.get("weight", 1.0))


def select_node(report: dict, engine: str, *, preferred: str | None = None) -> dict:
    nodes = report.get("nodes", [])
    if preferred:
        candidate = next((node for node in nodes if node["node_id"] == preferred), None)
        if candidate is None:
            raise ValueError(f"unknown cluster node: {preferred}")
        if _score(candidate, engine) == float("-inf"):
            raise ValueError(f"preferred node {preferred} cannot run {engine}")
        return candidate
    eligible = [(score, node) for node in nodes if (score := _score(node, engine)) > float("-inf")]
    if not eligible:
        raise RuntimeError(f"no cluster node is ready for {engine}")
    eligible.sort(key=lambda pair: (pair[0], pair[1]["node_id"]), reverse=True)
    return eligible[0][1]


def _remote_path(root: str, relative: str) -> str:
    value = relative.replace("/", "\\")
    if value.startswith("\\") or ".." in Path(value).parts or ":" in value:
        raise ValueError("remote output path must be a relative path")
    return str(Path(root) / value)


def worker_call(
    node: ClusterNode,
    engine: str,
    action: str,
    body: dict | None = None,
    *,
    timeout: float = 1_800,
) -> dict:
    if engine not in _PORTS:
        raise ValueError(f"unknown cluster engine: {engine}")
    paths = {
        "status": "/v1/status",
        "preflight": "/v1/preflight",
        "load": "/v1/load",
        "unload": "/v1/unload",
        "generate": "/v1/generate",
    }
    if action not in paths:
        raise ValueError(f"unknown worker action: {action}")
    port = _PORTS[engine]
    raw = base64.b64encode(json.dumps(body or {}, separators=(",", ":")).encode()).decode()
    method = "Get" if action in {"status", "preflight"} else "Post"
    script = f"""
$ProgressPreference = 'SilentlyContinue'
$uri = 'http://127.0.0.1:{port}{paths[action]}'
try {{
  if ('{method}' -eq 'Get') {{
    $response = Invoke-RestMethod -Uri $uri -TimeoutSec {max(5, int(timeout))} -Method Get
  }} else {{
    $bytes = [Convert]::FromBase64String('{raw}')
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    $response = Invoke-RestMethod -Uri $uri -TimeoutSec {max(5, int(timeout))} -Method Post -ContentType 'application/json' -Body $text
  }}
  $json = $response | ConvertTo-Json -Compress -Depth 24
  if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 4194304) {{
    throw 'Remote worker response exceeds 4 MiB'
  }}
  $json
}} catch {{
  $details = $_.ErrorDetails.Message
  if ($details) {{
    try {{
      ($details | ConvertFrom-Json) | ConvertTo-Json -Compress -Depth 24
    }} catch {{
      [pscustomobject]@{{ok=$false;errors=@([pscustomobject]@{{code='remote_worker_error';message=$details}})}} | ConvertTo-Json -Compress -Depth 8
    }}
  }} else {{
    [pscustomobject]@{{ok=$false;errors=@([pscustomobject]@{{code='remote_worker_error';message=$_.Exception.Message}})}} | ConvertTo-Json -Compress -Depth 8
  }}
}}
"""
    return _ssh(node, script, timeout=timeout + 15)


def deploy_node(
    node: ClusterNode,
    *,
    stage_models: bool = False,
    skip_runtime_install: bool = False,
    timeout: float = 7_200,
) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "deploy-cluster-node.ps1"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Node",
        node.node_id,
    ]
    if stage_models:
        command.append("-StageModels")
    if skip_runtime_install:
        command.append("-SkipRuntimeInstall")
    result = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"{node.node_id} deployment failed ({result.returncode}): {detail[-4000:]}"
        )
    output = result.stdout.strip()
    return {
        "ok": True,
        "node_id": node.node_id,
        "stage_models": stage_models,
        "output": output[-4000:],
    }


def generate(
    config: str | Path | None,
    engine: str,
    request: dict,
    *,
    preferred: str | None = None,
    fallback: bool = True,
) -> dict:
    nodes = load_nodes(config)
    topology = probe(nodes)
    selected = select_node(topology, engine, preferred=preferred)
    ordered = [selected]
    if fallback:
        for node in sorted(
            topology["nodes"],
            key=lambda item: _score(item, engine),
            reverse=True,
        ):
            if node["node_id"] != selected["node_id"] and _score(node, engine) > float("-inf"):
                ordered.append(node)
    failures = []
    by_id = {node.node_id: node for node in nodes}
    for candidate in ordered:
        node = by_id[candidate["node_id"]]
        try:
            body = dict(request)
            if body.get("output"):
                body["output"] = body["output"].replace("/", "\\")
            receipt = worker_call(node, engine, "generate", body)
            if receipt.get("ok") is True and receipt.get("synthesizedSpeech") is True:
                return {
                    **receipt,
                    "cluster": {
                        "selected_node": node.node_id,
                        "selected_score": _score(candidate, engine),
                        "attempted_nodes": [item["node_id"] for item in ordered[: len(failures) + 1]],
                        "failures": failures,
                    },
                }
            failures.append({"node_id": node.node_id, "receipt": receipt})
        except Exception as exc:
            failures.append({"node_id": node.node_id, "error": str(exc)})
    return {
        "ok": False,
        "synthesizedSpeech": False,
        "engine": engine,
        "cluster": {"failures": failures},
    }


def benchmark_manifest(report: dict) -> dict:
    """Stable evidence for a placement decision."""
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": digest,
        "report": report,
    }

