"""One product version everywhere (DD-8 uprev to 0.2.0).

Older bug, found in DD-8: the MCP serverInfo and the ACP agentInfo were the
literal "0.1.0" instead of the crate version, so an uprev of Cargo.toml would
have left the running server announcing the old version.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPECTED = "0.2.0"


def _versions() -> dict[str, str]:
    cargo = tomllib.loads((REPO / "Cargo.toml").read_text(encoding="utf-8"))
    py = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pkg = json.loads((REPO / "apps/desktop/package.json").read_text(encoding="utf-8"))
    lock = json.loads((REPO / "apps/desktop/package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads((REPO / "apps/desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    init = re.search(r'__version__ = "([^"]+)"', (REPO / "src/gen_audio/__init__.py").read_text(encoding="utf-8"))
    uv = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    uv_self = next(p["version"] for p in uv["package"] if p["name"] == "gen-audio")
    return {
        "Cargo.toml workspace.package": cargo["workspace"]["package"]["version"],
        "pyproject.toml": py["project"]["version"],
        "src/gen_audio/__init__.py": init.group(1) if init else "",
        "uv.lock gen-audio": uv_self,
        "apps/desktop/package.json": pkg["version"],
        "apps/desktop/package-lock.json": lock["version"],
        "apps/desktop/package-lock.json packages['']": lock["packages"][""]["version"],
        "apps/desktop/src-tauri/tauri.conf.json": tauri["version"],
    }


def test_every_version_source_is_the_release_version():
    versions = _versions()
    wrong = {name: v for name, v in versions.items() if v != EXPECTED}
    assert not wrong, f"expected {EXPECTED} everywhere: {wrong}"


def test_mcp_and_acp_announce_the_crate_version_not_a_literal():
    for rel, key in (("crates/gen-audio-mcp/src/lib.rs", "serverInfo"), ("crates/gen-audio-acp/src/lib.rs", "agentInfo")):
        text = (REPO / rel).read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if f'"{key}"' in l)
        assert 'env!("CARGO_PKG_VERSION")' in line, f"{rel}: {line.strip()}"
