"""Offline, non-destructive package migration; no models, imports or installs."""

import json
from pathlib import Path
import unittest

from node.packages import package_directory
from multiagent.storage import StoreLease
from session import SessionStore
from session.package_migration import migrate_workspace
from workspace_support import temporary_workspace


class PackageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(temporary_workspace(prefix="recoding-package-migration-").name)
        self.a=SessionStore.create(self.root,"a",{})
        self.b=SessionStore.create(self.root,"b",{})
        self.environment={"executable":"/usr/bin/python3","version":"3.12.3","implementation":"cpython","cache_tag":"cpython-312","platform":"linux-x86_64"}
        self.sources=[store.folder/"runtime/python-packages" for store in (self.a,self.b)]
        for source in self.sources:source.mkdir(parents=True)

    def distribution(self,source,name,version):
        record=source/f"{name}-{version}.dist-info";record.mkdir()
        (record/"METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")

    def test_merge_identical_and_disjoint_files_preserves_originals(self):
        for source in self.sources:
            self.distribution(source,"shared","1.0")
            (source/"shared.py").write_text("VALUE=42\n")
        (self.sources[1]/"extra.txt").write_text("resource")
        result=migrate_workspace(self.root,self.sources,self.environment)
        self.assertEqual(result["status"],"migrated",result)
        self.assertEqual((package_directory(self.root)/"extra.txt").read_text(),"resource")
        self.assertTrue(all((source/"shared.py").exists() for source in self.sources))
        inode=package_directory(self.root).stat().st_ino
        repeated=migrate_workspace(self.root,self.sources,self.environment)
        self.assertEqual(repeated["status"],"unchanged",repeated)
        self.assertEqual(package_directory(self.root).stat().st_ino,inode)
        self.assertEqual(json.loads((package_directory(self.root).parent/"python.json").read_text())["cache_tag"],"cpython-312")

    def test_version_and_file_conflicts_leave_target_and_sources_unchanged(self):
        self.distribution(self.sources[0],"shared","1.0")
        self.distribution(self.sources[1],"shared","2.0")
        result=migrate_workspace(self.root,self.sources,self.environment)
        self.assertEqual(result["status"],"blocked")
        self.assertIn("version conflict",result["errors"][0])
        self.assertFalse(package_directory(self.root).exists())
        # A different workspace verifies file-level conflicts independently.
        self.sources[0].joinpath("same.py").write_text("first")
        target=package_directory(self.root);target.mkdir()
        (target/"same.py").write_text("existing")
        result=migrate_workspace(self.root,[self.sources[0]],self.environment)
        self.assertEqual(result["status"],"blocked")
        self.assertIn("File conflict",result["errors"][0])
        self.assertEqual((target/"same.py").read_text(),"existing")

    def test_running_archive_lock_blocks_migration(self):
        (self.sources[0]/"kept.py").write_text("VALUE=42")
        with StoreLease(self.a.folder):result=migrate_workspace(self.root,self.sources,self.environment)
        self.assertEqual(result["status"],"blocked")
        self.assertFalse(package_directory(self.root).exists())
        self.assertTrue((self.sources[0]/"kept.py").exists())
