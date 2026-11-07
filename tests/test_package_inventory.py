"""Offline maintenance inventory: no user history, imports, Host or package installs."""

from contextlib import redirect_stdout, redirect_stderr
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from node.packages import package_directory, prepare_packages
from session import SessionStore
from session.package_inventory import scan_packages
from workspace_support import PROJECT, temporary_workspace


_spec = importlib.util.spec_from_file_location("recoding_package_maintenance", PROJECT / "maintenance/recoding-packages.py")
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)
main = maintenance.main


class PackageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace(prefix="recoding-package-inventory-")
        self.root = Path(self.temp.name)
        self.environment = {"executable": "/usr/bin/python3", "version": "3.12.3", "implementation": "cpython",
            "cache_tag": "cpython-312", "platform": "linux-x86_64", "user_site": str(self.root / "host-site")}
        self.a = SessionStore.create(self.root / "workspace", "one", {})
        self.b = SessionStore.create(self.root / "workspace-two", "two", {})
        self.a_second = SessionStore.create(self.root / "workspace", "same-workspace", {})
        self.sites = [prepare_packages(package_directory(store.workspace), self.environment) for store in (self.a, self.b)]

    def distribution(self, site, name, version, direct=None):
        record = site / f"{name}-{version}.dist-info"
        record.mkdir(parents=True)
        (record / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Dist: child>=1\n")
        if direct:
            (record / "direct_url.json").write_text(json.dumps(direct))

    def test_grouping_conflicts_user_versions_and_safe_candidates_without_running_code(self):
        for site in self.sites:
            self.distribution(site, "useful_tool", "1.0")
        self.distribution(self.sites[0], "numpy", "1.0")
        self.distribution(self.sites[1], "numpy", "2.0")
        self.distribution(self.sites[0], "already_shared", "3.0")
        self.distribution(self.sites[0], "different", "1.0")
        self.distribution(Path(self.environment["user_site"]), "already_shared", "3.0")
        self.distribution(Path(self.environment["user_site"]), "different", "2.0")
        self.distribution(self.sites[0], "local_edit", "1.0", {"url": "file:///private/source", "dir_info": {"editable": True}})
        (self.sites[0] / "unsafe.pth").write_text("import pathlib; pathlib.Path('SHOULD_NOT_RUN').touch()\n")
        (self.sites[0] / "unsafe.py").write_text("raise RuntimeError('must not import')")
        (self.a.folder / "state.json").write_text("NOT JSON; inventory must not read history")
        (self.root / "workspace" / "cycle").symlink_to(self.root, target_is_directory=True)
        before = {str(p): p.read_bytes() for p in self.root.rglob("METADATA")}
        report = scan_packages([self.root, self.root / "workspace"], environment=self.environment)
        self.assertTrue(report["complete"], report["errors"])
        self.assertEqual(len(report["sessions"]), 3)
        rows = {item["name"]: item for item in report["packages"]}
        self.assertEqual(rows["useful-tool"]["workspace_count"], 2)
        self.assertEqual(rows["numpy"]["status"], "conflicting-workspace-versions")
        self.assertEqual(rows["already-shared"]["status"], "already-shared")
        self.assertEqual(rows["different"]["status"], "different-user-version")
        self.assertEqual(rows["local-edit"]["status"], "needs-review")
        self.assertEqual(report["candidate_requirements"], ["useful-tool==1.0"])
        self.assertNotIn("file:///private/source", json.dumps(report))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("METADATA")})
        self.assertFalse((PROJECT / "SHOULD_NOT_RUN").exists())

    def test_system_packages_are_not_reported_as_missing_and_collection_review_is_separate(self):
        system=self.root/'system-site'
        self.environment['system_sites']=[str(system)]
        self.distribution(self.sites[0],'networkx','3.0')
        self.distribution(system,'networkx','3.0')
        self.distribution(self.sites[0],'system_old','2.0')
        self.distribution(system,'system_old','1.0')
        self.distribution(self.sites[0],'new_reusable','1.0')
        report=scan_packages([self.root],environment=self.environment)
        self.assertTrue(report['complete'])
        rows={row['name']:row for row in report['packages']}
        self.assertEqual(rows['networkx']['status'],'already-system')
        self.assertEqual(rows['networkx']['collection_status'],'tracked')
        self.assertEqual(rows['system-old']['status'],'different-system-version')
        self.assertEqual(rows['new-reusable']['status'],'missing-from-host')
        self.assertEqual(report['candidate_requirements'],['new-reusable==1.0'])
        self.assertIn('new-reusable',report['package_collection']['unreviewed'])
        self.assertNotIn('networkx',report['package_collection']['unreviewed'])

    def test_bad_metadata_legacy_packages_and_incompatible_interpreters_are_explicit(self):
        self.distribution(self.sites[0], "mismatch", "1.0")
        manifest = self.sites[0].parent / "python.json"
        manifest.write_text(json.dumps({**self.environment, "cache_tag": "cpython-310"}))
        legacy = self.root / "workspace" / ".python-packages"
        self.distribution(legacy, "legacy_only", "1.0")
        report = scan_packages([self.root], environment=self.environment)
        self.assertTrue(report["complete"], report)
        self.assertEqual(report["packages"][0]["status"], "needs-review")
        self.assertEqual(report["legacy_workspaces"][0]["packages"][0]["name"], "legacy_only")
        self.assertIn("legacy-only",report["host_missing_packages"])
        self.assertTrue(report["legacy_host_comparison"][0]["missing_from_host"])
        self.assertIn("legacy-only",report["package_collection"]["unreviewed"])
        self.assertEqual(report["candidate_requirements"], [])
        invalid = self.sites[1] / "broken-1.dist-info"
        invalid.mkdir()
        (invalid / "METADATA").symlink_to(self.a.folder / "session.json")
        report = scan_packages([self.root], environment=self.environment)
        self.assertFalse(report["complete"])
        self.assertTrue(any("broken-1" in item["path"] for item in report["errors"]))
        self.assertEqual(report["candidate_requirements"], [])
        bounded = scan_packages([self.root], environment=self.environment, max_directories=1)
        self.assertFalse(bounded["complete"])
        self.assertIn("limit", bounded["errors"][0]["error"])

    def test_command_supports_json_requirements_multiple_roots_and_no_host_startup(self):
        self.distribution(self.sites[0], "fixture", "1.0")
        with patch.object(maintenance, "scan_packages", side_effect=lambda roots: scan_packages(roots, environment=self.environment)):
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(["--root", str(self.root), "--root", str(self.root / "workspace"), "--json"]), 0)
            self.assertEqual(json.loads(stream.getvalue())["candidate_requirements"], ["fixture==1.0"])
            with redirect_stdout(stream := io.StringIO()):
                self.assertEqual(main(["--root", str(self.root), "--requirements"]), 0)
            self.assertIn("fixture==1.0", stream.getvalue())
            with redirect_stderr(stream := io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--root", str(self.root / "does-not-exist"), "--requirements"]), 1)
            self.assertIn("Incomplete", stream.getvalue())
        help_result = subprocess.run([sys.executable, "-B", "maintenance/recoding-packages.py", "--help"],
                                     cwd=PROJECT, capture_output=True, text=True, timeout=5)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--root", help_result.stdout)
