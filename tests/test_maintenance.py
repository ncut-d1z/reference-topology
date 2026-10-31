import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from reference_topology.config import Settings
from reference_topology.maintenance import create_backup, restore_backup, DATABASE_NAME
from reference_topology.storage import Store, StoreError


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.originals = self.base / "originals"
        self.originals.mkdir()
        self.settings = Settings(data_dir=self.base / "data", allowed_roots=[self.originals], host_id="original-host")
        self.store = Store(self.settings.data_dir / DATABASE_NAME)
        self.managed = self.settings.data_dir / "managed" / "fixture.pdf"
        self.managed.write_bytes(b"%PDF-1.4\nSynthetic managed fixture.\n%%EOF")
        self.a = self.register(self.managed, "managed")
        self.linked = self.originals / "linked.pdf"
        self.linked.write_bytes(b"%PDF-1.4\nExternal linked fixture.\n%%EOF")
        self.b = self.register(self.linked, "linked")
        self.output = self.settings.data_dir / "artifacts" / "parse.json"
        self.output.write_text('{"fixture": "raw parser evidence"}', encoding="utf-8")
        self.run = self.store.publish_parse(self.a["file_id"], self.a["paper_id"], {
            "engine": "test", "engine_version": "1", "status": "succeeded",
            "metadata": {"title": "A source paper", "doi": "10.1234/source"},
            "references": [{"ordinal": 1, "raw_text": "[1] Target. doi:10.1234/target",
                            "parsed": {"doi": "10.1234/target", "title": "Target"}}],
            "output_sha256": hashlib.sha256(self.output.read_bytes()).hexdigest(),
        }, output_uri=self.output.as_uri())

    def register(self, path, kind):
        data = path.read_bytes()
        return self.store.register_asset(hashlib.sha256(data).hexdigest(), len(data), path.as_uri(),
            None if kind == "managed" else path.as_uri(), path.name, self.settings.host_id,
            "managed" if kind == "managed" else self.settings.root_id(self.originals), kind)

    def backup(self):
        return create_backup(self.settings, self.store)

    def rewrite(self, source, name, transform):
        target = self.base / name
        with zipfile.ZipFile(source) as original:
            members = {entry.filename: original.read(entry) for entry in original.infolist()}
        transform(members)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as modified:
            for member, content in members.items():
                modified.writestr(member, content)
        return target

    def test_backup_restore_relocates_managed_and_artifacts_and_preserves_evidence(self):
        job = self.store.create_job("parse", self.a["file_id"], self.a["paper_id"])
        backup = self.backup()
        destination = self.base / "restored"
        result = restore_backup(Path(backup["path"]), destination)
        restored_settings = Settings.load(data_dir=destination)
        restored = Store(destination / DATABASE_NAME)
        self.assertNotEqual(restored_settings.host_id, self.settings.host_id)
        self.assertEqual(restored_settings.host_id, result["host_id"])
        self.assertEqual(restored_settings.allowed_roots, [])
        self.assertEqual((destination / "managed" / self.managed.name).read_bytes(), self.managed.read_bytes())
        location = restored.get_asset(self.a["file_id"])["locations"][0]
        self.assertEqual(location["canonical_uri"], (destination / "managed" / self.managed.name).as_uri())
        self.assertEqual(location["host_id"], result["host_id"])
        self.assertEqual(location["status"], "available")
        linked_location = restored.get_asset(self.b["file_id"])["locations"][0]
        self.assertEqual(linked_location["status"], "missing")
        self.assertEqual(linked_location["original_uri"], self.linked.as_uri())
        self.assertEqual(linked_location["host_id"], "original-host")
        self.assertEqual(restored.graph()["edges"], self.store.graph()["edges"])
        self.assertEqual(restored.get_job(job["id"])["status"], "interrupted")
        with restored.connection() as con:
            run = con.execute("SELECT * FROM extraction_runs WHERE id=?", (self.run["run_id"],)).fetchone()
            self.assertEqual(run["output_uri"], (destination / "artifacts" / "parse.json").as_uri())
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE extraction_runs SET output_uri='bad' WHERE id=?", (run["id"],))
        self.assertTrue(self.linked.exists())

    def test_backup_excludes_sessions_unreferenced_files_and_linked_pdf_bytes(self):
        secret = "DO-NOT-COPY-LOCAL-SESSION-TOKEN"
        (self.settings.data_dir / "session.json").write_text(secret)
        (self.settings.data_dir / "artifacts" / "unreferenced.txt").write_text(secret)
        backup = self.backup()
        with zipfile.ZipFile(backup["path"]) as archive:
            names = archive.namelist()
            self.assertEqual(set(names), {DATABASE_NAME, "manifest.json", "managed/fixture.pdf", "artifacts/parse.json"})
            self.assertFalse(any(secret.encode() in archive.read(name) for name in names))
        self.assertFalse(backup["manifest"]["linked_files"]["included"])
        self.assertEqual(backup["manifest"]["linked_files"]["location_count"], 1)

    def test_nonempty_destination_is_not_modified(self):
        backup = self.backup()
        destination = self.base / "existing"
        destination.mkdir()
        sentinel = destination / "important.txt"
        sentinel.write_text("keep")
        with self.assertRaises(StoreError) as raised:
            restore_backup(Path(backup["path"]), destination)
        self.assertEqual(raised.exception.code, "restore_destination_not_empty")
        self.assertEqual(sentinel.read_text(), "keep")

    def test_empty_destination_is_supported(self):
        backup = self.backup()
        destination = self.base / "empty"
        destination.mkdir()
        restore_backup(Path(backup["path"]), destination)
        self.assertTrue((destination / DATABASE_NAME).is_file())

    def test_zip_slip_member_is_rejected_before_extraction(self):
        backup = self.backup()
        bad = self.rewrite(backup["path"], "zip-slip.zip", lambda members: members.update({"../escaped.txt": b"no"}))
        destination = self.base / "rejected"
        with self.assertRaises(StoreError):
            restore_backup(bad, destination)
        self.assertFalse((self.base / "escaped.txt").exists())
        self.assertFalse(destination.exists())

    def test_duplicate_members_and_symlinks_are_rejected(self):
        backup = self.backup()
        duplicate = self.base / "duplicate.zip"
        duplicate.write_bytes(Path(backup["path"]).read_bytes())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "a") as archive:
                archive.writestr("manifest.json", b"{}")
        with self.assertRaises(StoreError):
            restore_backup(duplicate, self.base / "duplicate-out")
        symlink = self.base / "symlink.zip"
        symlink.write_bytes(Path(backup["path"]).read_bytes())
        with zipfile.ZipFile(symlink, "a") as archive:
            entry = zipfile.ZipInfo("managed/link.pdf")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../outside.pdf")
        with self.assertRaises(StoreError):
            restore_backup(symlink, self.base / "symlink-out")

    def test_hash_corruption_is_detected_and_nothing_is_published(self):
        backup = self.backup()
        def corrupt(members):
            original = members["artifacts/parse.json"]
            members["artifacts/parse.json"] = b"X" + original[1:]
        bad = self.rewrite(backup["path"], "corrupt.zip", corrupt)
        destination = self.base / "corrupt-out"
        with self.assertRaises(StoreError) as raised:
            restore_backup(bad, destination)
        self.assertEqual(raised.exception.code, "backup_hash_mismatch")
        self.assertFalse(destination.exists())

    def test_tampered_schema_is_detected_even_when_manifest_hash_matches(self):
        backup = self.backup()
        def tamper(members):
            database = self.base / "tampered.sqlite3"
            database.write_bytes(members[DATABASE_NAME])
            with sqlite3.connect(database) as con:
                con.execute("DROP TRIGGER reference_entries_immutable")
            con.close()  # Flush the WAL before rebuilding the archive member.
            content = database.read_bytes()
            members[DATABASE_NAME] = content
            manifest = json.loads(members["manifest.json"])
            record = next(r for r in manifest["files"] if r["path"] == DATABASE_NAME)
            record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
            members["manifest.json"] = json.dumps(manifest).encode()
        bad = self.rewrite(backup["path"], "schema.zip", tamper)
        with self.assertRaises(StoreError) as raised:
            restore_backup(bad, self.base / "schema-out")
        self.assertEqual(raised.exception.code, "invalid_backup_schema")

    def test_missing_managed_mapping_cannot_silently_lose_content(self):
        backup = self.backup()
        def remove(members):
            del members["managed/fixture.pdf"]
            manifest = json.loads(members["manifest.json"])
            manifest["files"] = [r for r in manifest["files"] if r["path"] != "managed/fixture.pdf"]
            manifest["managed_locations"] = []
            members["manifest.json"] = json.dumps(manifest).encode()
        bad = self.rewrite(backup["path"], "missing.zip", remove)
        with self.assertRaises(StoreError):
            restore_backup(bad, self.base / "missing-out")

    def test_artifact_outside_data_directory_is_not_read_into_backup(self):
        outside = self.originals / "private.json"
        outside.write_text("private")
        other = self.register(self.linked, "linked")
        self.store.publish_parse(other["file_id"], other["paper_id"],
            {"metadata": {}, "references": [], "status": "succeeded", "engine": "test", "engine_version": "1"},
            output_uri=outside.as_uri())
        with self.assertRaises(StoreError) as raised:
            self.backup()
        self.assertEqual(raised.exception.code, "backup_path_not_allowed")
        self.assertEqual(list((self.settings.data_dir / "backups").glob("*.zip")), [])

    def test_expansion_resource_limit_is_enforced(self):
        backup = self.backup()
        with patch("reference_topology.maintenance.MAX_TOTAL_BYTES", 10):
            with self.assertRaises(StoreError) as raised:
                restore_backup(Path(backup["path"]), self.base / "limited-out")
        self.assertEqual(raised.exception.code, "backup_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
