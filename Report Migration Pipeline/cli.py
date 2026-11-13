"""Command-line workflow for vendor-neutral, offline report migration drafts."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .audit import audit_tree
from .bundles import build, verify, write_new
from .contracts import InvalidInput, json_bytes, read_json, scaffold


def parser():
    result = argparse.ArgumentParser(description="Build reviewable SQL/JSON from explicit mappings and fictional or privately supplied normalized report inputs.")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an unmapped plan; no field mappings are guessed")
    init.add_argument("--report", type=Path, required=True)
    init.add_argument("--output", type=Path, required=True)
    generate = commands.add_parser("build", help="Validate and create a new deterministic review bundle")
    generate.add_argument("--project", type=Path, required=True)
    generate.add_argument("--out", type=Path, default=Path(".artifacts"))
    check = commands.add_parser("verify", help="Check hashes, optionally reproduce from the original inputs")
    check.add_argument("bundle", type=Path)
    check.add_argument("--project", type=Path)
    audit = commands.add_parser("audit", help="Scan local publication files without sending data anywhere")
    audit.add_argument("--root", type=Path, default=Path("."))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            report, _ = read_json(args.report)
            output = args.output.parent.resolve() / args.output.name
            write_new(output, json_bytes(scaffold(report)))
            outcome = {"status": "created", "output": str(output), "review_status": "unmapped"}
        elif args.command == "build":
            folder, state = build(args.project, args.out)
            outcome = {"status": state, "bundle": str(folder), "business_approval": "pending"}
        elif args.command == "verify":
            outcome = verify(args.bundle, args.project)
        else:
            outcome = audit_tree(args.root)
            print(json.dumps(outcome, indent=2))
            return 0 if outcome["status"] == "passed" else 1
    except (InvalidInput, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(outcome, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
