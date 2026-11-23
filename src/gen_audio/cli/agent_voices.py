"""CLI: import Yumlog Studio AgentVoice sets, or check the committed catalog.

    python -m gen_audio.cli.agent_voices import <dir with agent-voices.json> --set <name>
    python -m gen_audio.cli.agent_voices check [--require-wavs]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gen_audio.agent_voices import CATALOG_DIR, CATALOG_NAME, LIBRARY, AgentVoiceSourceError, check_catalog, import_set, write_catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Yumlog Studio AgentVoice import (asset object model v1).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("import", help="Import one Yumlog agent-voices.json folder (its WAVs beside it).")
    one.add_argument("source", type=Path, help="Folder holding agent-voices.json and the per-voice WAVs.")
    one.add_argument("--set", dest="set_name", required=True, help="Set name, [a-z0-9-], e.g. studio-ignite.")
    one.add_argument("--library", type=Path, default=LIBRARY)
    check = sub.add_parser("check", help="Re-mint every uid and verify counts and committed media.")
    check.add_argument("--library", type=Path, default=LIBRARY)
    check.add_argument("--require-wavs", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "import":
        try:
            imported = import_set(args.source, args.set_name, args.library)
        except AgentVoiceSourceError as error:
            print(f"agent_voices: {error}", file=sys.stderr)
            return 2
        path = write_catalog([imported], args.library)
        rows = imported["voices"]
        summary = {
            "catalog": str(path),
            "set": args.set_name,
            "source_voices": imported["source"]["voice_count"],
            "imported_rows": len(rows),
            "with_audio": sum(1 for row in rows if row["status"] == "ok"),
            "unavailable": [row["alias"] for row in rows if row["status"] != "ok"],
            "with_spectrogram": sum(1 for row in rows if row.get("spectrogram_uid")),
            "with_cube": sum(1 for row in rows if row.get("cube_uid")),
            "assets": len(imported["assets"]),
        }
        print(json.dumps(summary, indent=2))
        problems = check_catalog(json.loads(path.read_text(encoding="utf-8")), args.library, require_wavs=True)
    else:
        path = args.library / CATALOG_DIR / CATALOG_NAME
        problems = check_catalog(json.loads(path.read_text(encoding="utf-8")), args.library, require_wavs=args.require_wavs)
    for problem in problems:
        print(f"agent_voices: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
