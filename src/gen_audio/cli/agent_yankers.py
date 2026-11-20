"""CLI: validate Agent Yankers documents, print a planned episode, probe stubs.

Does not place a call and does not synthesize speech.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gen_audio.shows.agent_yankers.errors import AgentYankersError
from gen_audio.shows.agent_yankers.manifest import assemble_episode, load_episode
from gen_audio.shows.agent_yankers.models import load_highlights, load_marks, load_premise, load_voiceover
from gen_audio.shows.agent_yankers.schema import dump_json, load_schema, read_json, validate_instance
from gen_audio.shows.agent_yankers.session import StubLiveKitTransport
from gen_audio.shows.agent_yankers.voiceover import StubVibeVoiceShowAudio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-audio-agent-yankers",
        description="Validate Agent Yankers documents and print a planned episode. Does not place calls or write audio.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    premise = sub.add_parser("validate-premise", help="validate a premise document")
    premise.add_argument("path", type=Path)
    premise.set_defaults(func=_validate_premise)

    marks = sub.add_parser("validate-marks", help="validate a marks index")
    marks.add_argument("path", type=Path)
    marks.set_defaults(func=_validate_marks)

    voiceover = sub.add_parser("validate-voiceover", help="validate a voiceover plan")
    voiceover.add_argument("path", type=Path)
    voiceover.set_defaults(func=_validate_voiceover)

    highlights = sub.add_parser("validate-highlights", help="validate a highlight list")
    highlights.add_argument("path", type=Path)
    highlights.set_defaults(func=_validate_highlights)

    session = sub.add_parser("validate-session", help="validate a call session document")
    session.add_argument("path", type=Path)
    session.set_defaults(func=_validate_session)

    episode = sub.add_parser("validate-episode", help="validate an episode or highlight reel")
    episode.add_argument("path", type=Path)
    episode.add_argument("--marks", type=Path, help="also refuse call segments whose mark lacks consent")
    episode.add_argument("--records", type=Path, help="consent records directory (required with --marks)")
    episode.set_defaults(func=_validate_episode)

    plan = sub.add_parser("plan", help="print a planned episode manifest")
    plan.add_argument("--premise", type=Path, required=True)
    plan.add_argument("--marks", type=Path, required=True)
    plan.add_argument("--voiceover", type=Path, required=True)
    plan.add_argument("--highlights", type=Path, required=True)
    plan.add_argument("--records", type=Path, required=True)
    plan.add_argument("--episode-id", required=True)
    plan.add_argument("--title", required=True)
    plan.set_defaults(func=_plan)

    probe = sub.add_parser("probe", help="print LiveKit and VibeVoice stub status")
    probe.set_defaults(func=_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, AgentYankersError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _validate_premise(args) -> int:
    premise = load_premise(args.path)
    print(f"ok premise {premise.id} targets={len(premise.targets)}")
    return 0


def _validate_marks(args) -> int:
    index = load_marks(args.path)
    print(f"ok marks {len(index.marks)}")
    return 0


def _validate_voiceover(args) -> int:
    plan = load_voiceover(args.path)
    print(f"ok voiceover cues={len(plan.cues)}")
    return 0


def _validate_highlights(args) -> int:
    rows = load_highlights(args.path)
    print(f"ok highlights {len(rows)}")
    return 0


def _validate_session(args) -> int:
    document = read_json(args.path)
    validate_instance(document, load_schema("session"))
    print(f"ok session {document['premise_id']} {document['mark_id']}")
    return 0


def _validate_episode(args) -> int:
    if (args.marks is None) != (args.records is None):
        raise AgentYankersError("pass both --marks and --records, or neither", code="episode")
    marks = load_marks(args.marks) if args.marks is not None else None
    document = load_episode(args.path, marks=marks, records_root=args.records)
    print(f"ok {document['kind']} {document['id']} segments={len(document['segments'])}")
    return 0


def _plan(args) -> int:
    episode = assemble_episode(
        episode_id=args.episode_id,
        title=args.title,
        premise=load_premise(args.premise),
        marks=load_marks(args.marks),
        voiceover=load_voiceover(args.voiceover),
        highlights=load_highlights(args.highlights),
        records_root=args.records,
    )
    sys.stdout.write(dump_json(episode))
    return 0


def _probe(_args) -> int:
    sys.stdout.write(
        dump_json(
            {
                "livekit": StubLiveKitTransport().probe().as_dict(),
                "vibevoice": StubVibeVoiceShowAudio().probe().as_dict(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
