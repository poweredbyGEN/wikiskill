from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .core import (
    WikiSkillError,
    configure_group,
    context,
    evolve,
    evolve_group,
    ingest,
    load_registry,
    project_for_path,
    recent_listing,
    register,
    run,
    state_dir,
)
from .proof import proof, write_report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="wikiskill",
        description="Group-scoped WikiSkill evolution based on arXiv:2608.27454",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    add = commands.add_parser(
        "register", help="register one Git repository in a knowledge group"
    )
    add.add_argument("path", nargs="?", default=".")
    add.add_argument("--group", default="default")
    add.add_argument("--deja-project", default="")
    add.add_argument("--model", default="")
    add.add_argument("--graphify-mcp", default="")
    add.add_argument("--graphify-host", default="")
    add.add_argument("--validator", default="")
    configure = commands.add_parser(
        "configure-group", help="update shared model or validator settings for a group"
    )
    configure.add_argument("path", nargs="?", default=".")
    configure.add_argument("--model", default=None)
    configure.add_argument("--validator", default=None)

    for name in ("ingest", "evolve"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
        command.add_argument("--since", default="36h")
        command.add_argument("--quiet-minutes", type=int, default=30)

    nightly = commands.add_parser(
        "nightly", help="evolve groups whose registered repositories have new sessions"
    )
    nightly.add_argument("--since", default="36h")
    nightly.add_argument("--quiet-minutes", type=int, default=30)
    nightly.add_argument("--warmup", action="store_true")

    for name in ("status", "context"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
    hook = commands.add_parser(
        "hook-context", help="fail-open session-start context for agent hooks"
    )
    hook.add_argument("path", nargs="?", default=".")
    validate = commands.add_parser(
        "proof", help="score held-out agent tasks with deterministic verifiers"
    )
    validate.add_argument("manifest", nargs="?", default=".wikiskill-proof.toml")
    validate.add_argument("--skills", default="")
    validate.add_argument("--project", default=".")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "register":
            project = register(
                args.path,
                group_id=args.group,
                deja_project=args.deja_project,
                model=args.model,
                graphify_mcp=args.graphify_mcp,
                graphify_host=args.graphify_host,
                validator_command=args.validator,
            )
            print(
                json.dumps(
                    {
                        "registered": project.project_id,
                        "group": project.group_id,
                        "root": project.root,
                        "state": str(state_dir(project.group_id)),
                    }
                )
            )
        elif args.command == "configure-group":
            projects = configure_group(
                args.path,
                model=args.model,
                validator_command=args.validator,
            )
            print(
                json.dumps(
                    {
                        "group": projects[0].group_id,
                        "updated": [project.project_id for project in projects],
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "ingest":
            project = project_for_path(args.path)
            sessions = ingest(project, args.since, args.quiet_minutes)
            print(
                json.dumps(
                    {
                        "project": project.project_id,
                        "group": project.group_id,
                        "ingested": len(sessions),
                    }
                )
            )
        elif args.command == "evolve":
            project = project_for_path(args.path)
            print(
                json.dumps(
                    evolve(project, args.since, args.quiet_minutes), sort_keys=True
                )
            )
        elif args.command == "nightly":
            if args.warmup:
                run(["deja", "warmup"], timeout=1800)
            projects = load_registry()
            listing = recent_listing(args.since)
            failed = False
            for group_id in sorted({project.group_id for project in projects.values()}):
                try:
                    print(
                        json.dumps(
                            evolve_group(
                                group_id,
                                listing,
                                args.quiet_minutes,
                            ),
                            sort_keys=True,
                        )
                    )
                except (
                    WikiSkillError,
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    print(
                        json.dumps({"group": group_id, "error": str(exc)})
                    )
                    failed = True
            return 1 if failed else 0
        elif args.command == "status":
            project = project_for_path(args.path)
            root = state_dir(project.group_id)
            manifest = json.loads(
                (root / "raw/manifest.json").read_text(encoding="utf-8")
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {
                        "project": project.project_id,
                        "group": project.group_id,
                        "root": project.root,
                        "state": str(root),
                        "raw_sessions": len(manifest["sessions"]),
                        "processed_sessions": len(state["processed_sessions"]),
                        "best_score": state["best_score"],
                        "skills": len(list((root / "skills").glob("*/SKILL.md"))),
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "context":
            print(context(project_for_path(args.path)), end="")
        elif args.command == "hook-context":
            try:
                print(context(project_for_path(args.path)), end="")
            except (
                WikiSkillError,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                KeyError,
                json.JSONDecodeError,
            ):
                pass
        elif args.command == "proof":
            skills = args.skills or os.environ.get("WIKISKILL_SKILLS_DIR", "")
            if not skills:
                raise WikiSkillError("proof needs --skills or WIKISKILL_SKILLS_DIR")
            result = proof(args.manifest, skills, args.project)
            report = os.environ.get("WIKISKILL_VALIDATION_REPORT", "")
            if report:
                write_report(report, result)
            for case in result["cases"]:
                print(
                    f"proof {case['name']}#{case['run']}: {case['outcome']}",
                    file=sys.stderr,
                )
            print(json.dumps(result, sort_keys=True))
        return 0
    except (WikiSkillError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"wikiskill: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
