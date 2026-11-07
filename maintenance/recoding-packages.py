#!/usr/bin/env python3
"""Host Coding Agent maintenance command; no Session Host or UI is started."""

import argparse
import json
from pathlib import Path
import sys

from session.package_inventory import scan_packages


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect shared packages across local Recoding workspaces; never install anything.")
    parser.add_argument("--root", action="append", help="Search root; repeat for multiple locations. Default: current user's home.")
    parser.add_argument("--exclude", action="append", default=[], help="Skip this subtree; repeat to exclude retained test/backup data.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Full machine-readable inventory, including conflicts and provenance hints.")
    output.add_argument("--requirements", action="store_true", help="Review candidates only: host-missing, single-version, compatible packages. Not an installation approval.")
    parser.add_argument("--migrate", action="store_true", help="Offline conflict-checked merge of legacy package directories; keep all originals.")
    args = parser.parse_args(argv)
    if args.migrate and args.requirements:
        parser.error("--migrate cannot be combined with --requirements")
    try:
        report = scan_packages(args.root or [str(Path.home())], **({"exclude":args.exclude} if args.exclude else {}))
    except (OSError, ValueError) as exc:
        print(f"Package scan failed: {exc}", file=sys.stderr)
        return 2
    if args.migrate:
        from collections import defaultdict
        from node.packages import python_environment
        from session.package_migration import migrate_workspace
        if not report["complete"]:
            print("Incomplete scan; resolve --json errors before migration.",file=sys.stderr)
            return 1
        sources=defaultdict(list)
        for item in report["legacy_workspaces"]+report["legacy_session_packages"]:
            sources[item["workspace"]].append(item["package_directory"])
        environment=python_environment()
        outcomes=[migrate_workspace(workspace,paths,environment) for workspace,paths in sorted(sources.items())]
        report=scan_packages(args.root or [str(Path.home())], **({"exclude":args.exclude} if args.exclude else {}))
        report["migration_results"]=outcomes
        report["complete"]=report["complete"] and all(item["status"]!="blocked" for item in outcomes)
        if not report["complete"]:report["candidate_requirements"]=[]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.requirements:
        if not report["complete"]:
            print("Incomplete inventory; inspect --json errors before generating candidates.", file=sys.stderr)
            return 1
        print("# Review dependency conflicts and provenance before installing into the tool Python user site.")
        print("# Existing public versions are never included for automatic replacement.")
        print("\n".join(report["candidate_requirements"]))
    else:
        print(f"Workspaces: {len(report['workspaces'])} | Sessions: {len(report['sessions'])} | Packages: {len(report['packages'])} | Complete: {report['complete']}")
        print(f"Tool Python: {report['tool_python']['executable']} ({report['tool_python']['version']})")
        print(f"Host user site: {report['tool_python']['user_site']}")
        print("Host system sites: " + ", ".join(report["tool_python"].get("system_sites", [])))
        for row in report["packages"]:
            print(f"{row['name']:<32} {', '.join(row['versions']):<20} {row['status']} | {row['workspace_count']} workspaces / {row['session_count']} Sessions")
        unreviewed = report.get("package_collection",{}).get("unreviewed",[])
        if unreviewed:print("Not in project collection (review reusable value; do not auto-promote dependencies): " + ", ".join(unreviewed))
        for item in report.get("migration_results",[]):
            print(f"Migration {item['status']}: {item['workspace']} {item['errors']}")
        if report["legacy_session_packages"]:
            print(f"Legacy Session package directories: {len(report['legacy_session_packages'])}; --migrate checks conflicts and preserves originals.")
        if report["legacy_workspaces"]:
            print(f"Legacy workspace package directories: {len(report['legacy_workspaces'])}; inspect --json, ownership unknown.")
        for error in report["errors"]:
            print(f"{error['path']}: {error['error']}", file=sys.stderr)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
