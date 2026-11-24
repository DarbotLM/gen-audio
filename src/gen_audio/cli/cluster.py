"""Probe and route 7B generation jobs across native Windows nodes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gen_audio.cluster import (
    deploy_node,
    generate,
    load_nodes,
    probe,
    select_node,
    worker_call,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", default=None)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    item = sub.add_parser("deploy")
    item.add_argument("--node", choices=("all", "dayour", "darbotlm", "darbotlabs"), default="all")
    item.add_argument("--stage-models", action="store_true")
    item.add_argument("--skip-runtime-install", action="store_true")
    for name in ("load", "unload", "preflight"):
        item = sub.add_parser(name)
        item.add_argument("--engine", choices=("vibevoice", "personaplex"), required=True)
        item.add_argument("--node")
    item = sub.add_parser("generate")
    item.add_argument("--engine", choices=("vibevoice", "personaplex"), required=True)
    item.add_argument("--node")
    item.add_argument("--request", type=Path, required=True)
    item.add_argument("--no-fallback", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        nodes = load_nodes(args.config)
        if args.command == "status":
            result = probe(nodes)
        elif args.command == "deploy":
            selected = nodes if args.node == "all" else tuple(
                node for node in nodes if node.node_id == args.node
            )
            result = {
                "ok": True,
                "nodes": [
                    deploy_node(
                        node,
                        stage_models=args.stage_models,
                        skip_runtime_install=args.skip_runtime_install,
                    )
                    for node in selected
                ],
            }
        elif args.command == "generate":
            request = json.loads(args.request.read_text(encoding="utf-8-sig"))
            result = generate(
                args.config,
                args.engine,
                request,
                preferred=args.node,
                fallback=not args.no_fallback,
            )
        else:
            topology = probe(nodes)
            chosen = select_node(topology, args.engine, preferred=args.node)
            node = next(item for item in nodes if item.node_id == chosen["node_id"])
            result = worker_call(node, args.engine, args.command)
            result["cluster"] = {"selected_node": node.node_id, "action": args.command}
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [{"code": "cluster_failed", "message": str(exc)}]}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
