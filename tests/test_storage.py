"""Behavioural tests for persistence, evidence, curation, and worker fencing."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reference_topology.storage import Store, StoreError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalogue.sqlite3"
        self.store = Store(self.path)

    def asset(self, name="source", content=None, uri=None):
        data = (content or name).encode()
        return self.store.register_asset(hashlib.sha256(data).hexdigest(), len(data),
                                         uri or f"file:///papers/{name}.pdf", uri or f"file:///papers/{name}.pdf",
                                         name + ".pdf", "host-1", "papers", "linked")

    @staticmethod
    def ref(doi="10.1234/b", title="A reliable target paper", raw=None, **extra):
        parsed = {"title": title, "doi": doi, "year": 2020, **extra}
        return {"ordinal": 1, "raw_text": raw or f"[1] {title}. 2020. doi:{doi}",
                "parsed": parsed, "locations": [{"page": 3}]}

    def parse(self, asset, refs=(), metadata=None, status="succeeded", **kwargs):
        return self.store.publish_parse(asset["file_id"], asset["paper_id"],
            {"status": status, "metadata": metadata or {"title": "Source paper", "doi": "10.1234/source"},
             "references": list(refs), "warnings": [], "page_count": 4,
             "engine": "test-parser", "engine_version": "1"}, **kwargs)

    def count(self, table):
        with self.store.connection() as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_migration_is_idempotent_and_schema_has_thirteen_tables(self):
        Store(self.path)
        with self.store.connection() as con:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertEqual(len(tables), 13)
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])


    def test_jobs_overview_does_not_hide_running_or_finished_behind_recent_queued(self):
        asset = self.asset("queue")
        created = []
        for index in range(130):
            job = self.store.create_job("parse", asset_id=asset["file_id"],
                paper_id=asset["paper_id"], request_key=f"jobs-test-{index}",
                work_key=f"jobs-work-{index}", input_data={"number": index})
            created.append(job)
        first = self.store.claim_job()
        self.assertEqual(first["id"], created[0]["id"])
        self.store.update_job(first["id"], lease_token=first["lease_token"],
                              status="succeeded", result={"ok": True})
        running = self.store.claim_job()
        self.assertEqual(running["id"], created[1]["id"])
        overview = self.store.jobs_overview(100)
        self.assertEqual(overview["total"], 130)
        self.assertEqual(overview["counts"]["succeeded"], 1)
        self.assertEqual(overview["counts"]["running"], 1)
        self.assertEqual(overview["counts"]["queued"], 128)
        self.assertTrue(overview["truncated"])
        ids = {item["id"] for item in overview["items"]}
        self.assertIn(first["id"], ids)
        self.assertIn(running["id"], ids)
        self.assertIn(created[2]["id"], ids)
        self.assertIn(created[-1]["id"], ids)
        self.assertEqual(len(ids), len(overview["items"]))
        self.assertLessEqual(len(ids), 100)

    def test_status_filter_queries_full_queue_and_pages_without_duplicates(self):
        asset = self.asset("filtered")
        jobs = [self.store.create_job("parse", asset_id=asset["file_id"],
                paper_id=asset["paper_id"], request_key=f"filter-request-{i}",
                work_key=f"filter-work-{i}", input_data={"n": i})
                for i in range(145)]
        first = self.store.claim_job()
        self.store.update_job(first["id"], lease_token=first["lease_token"], status="failed",
                              error_code="test", error_message="intentional")
        second = self.store.claim_job()
        self.store.update_job(second["id"], lease_token=second["lease_token"], status="succeeded")
        third = self.store.claim_job()
        self.store.cancel_job(jobs[-1]["id"])

        page1 = self.store.query_jobs(status="queued", limit=30, offset=0)
        page2 = self.store.query_jobs(status="queued", limit=30, offset=30)
        page3 = self.store.query_jobs(status="queued", limit=100, offset=60)
        queued_ids = [entry["id"] for entry in page1["items"] + page2["items"] + page3["items"]]
        self.assertEqual(page1["total"], 145)
        self.assertEqual(page1["filtered_total"], 141)
        self.assertEqual(page1["counts"]["queued"], 141)
        self.assertEqual(page1["counts"]["running"], 1)
        self.assertEqual(page1["counts"]["succeeded"], 1)
        self.assertEqual(page1["counts"]["failed"], 1)
        self.assertEqual(page1["counts"]["cancelled"], 1)
        self.assertEqual(len(queued_ids), 141)
        self.assertEqual(len(set(queued_ids)), 141)
        self.assertNotIn(jobs[-1]["id"], queued_ids)
        self.assertEqual(queued_ids[0], jobs[3]["id"])
        self.assertFalse(page3["truncated"])

        for status, expected in (("running", third["id"]), ("succeeded", second["id"]),
                                  ("failed", first["id"]), ("cancelled", jobs[-1]["id"])):
            filtered = self.store.query_jobs(status=status, limit=30)
            self.assertEqual(filtered["filtered_total"], 1)
            self.assertEqual([entry["id"] for entry in filtered["items"]], [expected])
            self.assertEqual(filtered["total"], 145)
        self.assertEqual(self.store.query_jobs("partial")["filtered_total"], 0)
        self.assertEqual(self.store.query_jobs("interrupted")["items"], [])
        all_jobs = self.store.query_jobs("all", limit=30)
        self.assertEqual(all_jobs["filtered_total"], 145)
        self.assertEqual(all_jobs["items"][0]["status"], "running")
        self.assertEqual(len(all_jobs["items"]), 30)
        self.assertTrue(all_jobs["truncated"])
        self.assertEqual(self.store.query_jobs("queued", limit=30, offset=999)["items"], [])
        with self.assertRaises(StoreError):
            self.store.query_jobs("unknown")

    def test_content_deduplication_locations_and_replaced_path_are_separate(self):
        first = self.asset("one", content="same", uri="file:///a.pdf")
        duplicate = self.asset("two", content="same", uri="file:///b.pdf")
        self.assertEqual(first["file_id"], duplicate["file_id"])
        self.assertEqual(first["paper_id"], duplicate["paper_id"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(len(self.store.get_asset(first["file_id"])["locations"]), 2)
        replaced = self.asset("new", content="different", uri="file:///a.pdf")
        self.assertNotEqual(first["file_id"], replaced["file_id"])
        previous = next(p for p in self.store.get_asset(first["file_id"])["locations"] if p["canonical_uri"] == "file:///a.pdf")
        self.assertEqual(previous["status"], "changed")
        self.assertEqual(previous["is_current"], 0)
        self.assertEqual(self.count("pdf_assets"), 2)

    def test_registered_files_survive_store_restart(self):
        a = self.asset()
        reopened = Store(self.path)
        self.assertEqual(reopened.get_asset(a["file_id"])["locations"][0]["canonical_uri"], "file:///papers/source.pdf")
        self.assertEqual(reopened.paper_detail(a["paper_id"])["file_id"], a["file_id"])

    def test_edges_deduplicate_evidence_and_have_matching_reverse_query(self):
        a = self.asset()
        r1 = self.ref()
        r2 = {**self.ref(), "ordinal": 2, "raw_text": "[2] A second raw occurrence. doi:10.1234/b"}
        self.parse(a, [r1, r2])
        refs = self.store.list_references(a["paper_id"])
        target = refs[0]["target"]["id"]
        graph = self.store.graph()
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["evidence_count"], 2)
        incoming = self.store.list_citations(target)
        self.assertEqual([r["id"] for r in incoming], [a["paper_id"]])
        self.assertEqual(len(self.store.edge_evidence(a["paper_id"], target)), 2)
        self.assertEqual(self.store.paper_detail(target)["incoming_count"], 1)
        self.assertFalse(self.store.paper_detail(target)["has_pdf"])

    def test_imported_pdf_completes_existing_reference_node(self):
        a = self.asset()
        self.parse(a, [self.ref()])
        target = self.store.list_references(a["paper_id"])[0]["target"]["id"]
        b = self.asset("target")
        result = self.parse(b, metadata={"title": "A reliable target paper", "doi": "10.1234/b", "year": 2020})
        self.assertEqual(result["paper_id"], target)
        self.assertEqual(self.store.list_papers()["total"], 2)
        self.assertEqual(self.store.paper_detail(target)["file_id"], b["file_id"])
        self.assertEqual(self.store.canonical_id(b["paper_id"]), target)
        # Re-import/reparse with the returned canonical ID must retire the same
        # original source snapshot rather than leaving parallel active runs.
        b["paper_id"] = target
        self.parse(b, metadata={"title": "A reliable target paper", "doi": "10.1234/b"})
        with self.store.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM extraction_runs WHERE asset_id=? AND is_active=1", (b["file_id"],)).fetchone()[0], 1)

    def test_automatic_pdf_identity_merge_is_immediately_reversible(self):
        target = self.store.create_paper({"doi": "10.1234/target"}, manual=False)
        b = self.asset("new-pdf")
        result = self.parse(b, metadata={"title": "The PDF supplies a missing title", "doi": "10.1234/target"})
        self.assertEqual(result["paper_id"], target["id"])
        self.assertEqual(self.store.paper_detail(target["id"])["title"], "The PDF supplies a missing title")
        self.assertEqual(self.store.list_papers(q="supplies a missing")["total"], 1)
        with self.store.connection() as con:
            merge_id = con.execute("SELECT id FROM paper_merge_events WHERE from_paper_id=?", (b["paper_id"],)).fetchone()[0]
        self.store.revert_merge(merge_id)
        self.assertIsNone(self.store.paper_detail(target["id"])["title"])
        self.assertEqual(self.store.paper_detail(b["paper_id"])["title"], "The PDF supplies a missing title")

    def test_manual_fields_and_rejections_survive_reparse(self):
        a = self.asset()
        self.parse(a, [self.ref()])
        entry = self.store.list_references(a["paper_id"])[0]
        target = entry["target"]["id"]
        self.store.edit_paper(a["paper_id"], {"title": "My corrected title"})
        self.store.decide_reference(entry["id"], "reject", target)
        self.parse(a, [self.ref()], metadata={"title": "An incorrect replacement", "doi": "10.1234/source"})
        current = self.store.list_references(a["paper_id"])[0]
        self.assertEqual(self.store.paper_detail(a["paper_id"])["title"], "My corrected title")
        self.assertEqual(current["status"], "rejected")
        self.assertEqual(self.store.graph()["citation_count"], 0)
        self.assertEqual(current["aligned_from_entry_id"], entry["id"])
        self.store.match_pending()
        self.assertEqual(self.store.graph()["citation_count"], 0)

    def test_manual_acceptance_is_inherited_only_for_unambiguous_same_text(self):
        a = self.asset()
        self.parse(a, [self.ref(doi=None, title="A title without an identifier")])
        entry = self.store.list_references(a["paper_id"])[0]
        target = self.store.create_paper({"title": "Manually identified target"})
        self.store.decide_reference(entry["id"], "accept", target["id"])
        self.parse(a, [self.ref(doi=None, title="A title without an identifier")])
        self.assertEqual(self.store.graph()["citation_count"], 1)
        self.parse(a, [self.ref(doi=None, title="Changed ambiguous reference text")])
        self.assertEqual(self.store.graph()["citation_count"], 0)
        self.assertTrue(any(r["status"] == "needs_review" for r in self.store.list_references(a["paper_id"])))

    def test_partial_or_failed_parse_preserves_complete_snapshot(self):
        a = self.asset()
        first = self.parse(a, [self.ref()])
        partial = self.parse(a, [], status="partial")
        self.assertFalse(partial["active"])
        self.parse(a, [], status="needs_ocr")
        refs = self.store.list_references(a["paper_id"])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["run_id"], first["run_id"])

    def test_manual_revision_cannot_be_overwritten_by_automatic_parse(self):
        a = self.asset()
        parsed = self.parse(a, [self.ref()])
        manual = self.store.revise_references(a["paper_id"], parsed["run_id"], [self.ref("10.1234/c", "Another reliable paper")])
        later = self.parse(a, [self.ref()])
        self.assertFalse(later["active"])
        self.assertIn("manual_revision_preserved", later["warnings"])
        self.assertEqual(self.store.list_references(a["paper_id"])[0]["run_id"], manual["run_id"])

    def test_weak_reference_boundary_never_becomes_an_accepted_edge(self):
        a = self.asset()
        self.parse(a, [self.ref(_needs_review=True)])
        entry = self.store.list_references(a["paper_id"])[0]
        self.assertFalse(entry["matched"])
        self.assertEqual(self.store.graph()["citation_count"], 0)
        self.assertGreater(len(self.store.graph(include_pending=True)["nodes"]), 2)

    def test_title_only_match_stays_candidate(self):
        self.store.create_paper({"title": "A reliable target paper", "year": 2020})
        a = self.asset()
        self.parse(a, [self.ref(doi=None)])
        entry = self.store.list_references(a["paper_id"])[0]
        self.assertEqual(entry["status"], "candidate")
        self.assertFalse(entry["matched"])

    def test_raw_evidence_is_immutable_and_wrong_source_is_rejected(self):
        a = self.asset()
        published = self.parse(a, [self.ref()])
        entry = self.store.list_references(a["paper_id"])[0]
        b = self.store.create_paper({"title": "Wrong source"})
        with self.store.connection() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE reference_entries SET raw_text='overwrite' WHERE id=?", (entry["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("""INSERT INTO reference_entries(id,run_id,source_paper_id,ordinal,raw_text,parsed_json,text_fingerprint,created_at)
                    VALUES('bad',?,?,99,'raw','{}','hash','now')""", (published["run_id"], b["id"]))
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_parse_publication_rolls_back_all_objects_on_invalid_metadata(self):
        a = self.asset()
        with self.assertRaises(StoreError):
            self.parse(a, [self.ref()], metadata={"title": "Should not remain", "year": "bad-year"})
        self.assertEqual(self.count("extraction_runs"), 0)
        self.assertEqual(self.count("reference_entries"), 0)
        self.assertIsNone(self.store.paper_detail(a["paper_id"])["title"])

    def test_merge_undo_and_later_edit_conflict(self):
        a = self.store.create_paper({"title": "First identity", "doi": "10.1234/first"})
        b = self.store.create_paper({"title": "Second identity", "doi": "10.1234/second"})
        event = self.store.merge_papers(a["id"], b["id"])
        self.assertEqual(self.store.canonical_id(a["id"]), b["id"])
        self.assertEqual(len(self.store.paper_detail(b["id"])["identifiers"]), 2)
        self.store.revert_merge(event["id"])
        self.assertEqual(self.store.canonical_id(a["id"]), a["id"])
        event = self.store.merge_papers(a["id"], b["id"])
        self.store.edit_paper(b["id"], {"title": "A subsequent edit"})
        with self.assertRaises(StoreError) as raised:
            self.store.revert_merge(event["id"])
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(self.store.paper_detail(b["id"])["title"], "A subsequent edit")

    def test_stale_paper_version_does_not_overwrite_manual_edit(self):
        p = self.store.create_paper({"title": "Initial"})
        self.store.edit_paper(p["id"], {"title": "Newer"}, p["row_version"])
        with self.assertRaises(StoreError):
            self.store.edit_paper(p["id"], {"title": "Stale"}, p["row_version"])
        self.assertEqual(self.store.paper_detail(p["id"])["title"], "Newer")

    def test_merge_induced_self_edge_is_reviewable_and_undo_restores_it(self):
        a = self.asset()
        self.parse(a, [self.ref()])
        target = self.store.list_references(a["paper_id"])[0]["target"]["id"]
        event = self.store.merge_papers(a["paper_id"], target)
        self.assertEqual(self.store.graph()["citation_count"], 0)
        self.assertEqual(self.store.list_references(target)[0]["status"], "needs_review")
        self.store.revert_merge(event["id"])
        self.assertEqual(self.store.graph()["citation_count"], 1)

    def test_graph_cycle_is_bounded_and_reverse_direction_is_consistent(self):
        a = self.asset("a")
        self.parse(a, [self.ref("10.1234/b", "A reliable target paper")])
        b = self.asset("b")
        result = self.parse(b, [self.ref("10.1234/source", "Source paper")],
                            metadata={"title": "A reliable target paper", "doi": "10.1234/b", "year": 2020})
        graph = self.store.graph(center=a["paper_id"], depth=3)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["edges"]), 2)
        self.assertFalse(graph["truncated"])
        inbound = self.store.graph(center=a["paper_id"], direction="in", depth=1)
        self.assertIn(result["paper_id"], {n["id"] for n in inbound["nodes"]})
        limited = self.store.graph(center=a["paper_id"], depth=3, limit=1)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["nodes"]), 1)

    def test_jobs_are_idempotent_and_concurrent_claim_is_exclusive(self):
        a = self.asset()
        first = self.store.create_job("parse", a["file_id"], a["paper_id"], "request-1", "work-1")
        alias = self.store.create_job("parse", a["file_id"], a["paper_id"], "request-2", "work-1")
        self.assertEqual(first["id"], alias["id"])
        with self.assertRaises(StoreError) as collision:
            self.store.create_job("other-operation", a["file_id"], a["paper_id"], "request-other", "work-1")
        self.assertEqual(collision.exception.code, "work_key_conflict")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: self.store.claim_job(), range(2)))
        claimed = [j for j in claimed if j]
        self.assertEqual(len(claimed), 1)
        job = claimed[0]
        self.store.update_job(job["id"], lease_token=job["lease_token"], status="succeeded", result={"ok": True})
        replay = self.store.create_job("parse", a["file_id"], a["paper_id"], "request-2", "work-1")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["status"], "succeeded")
        with self.assertRaises(StoreError):
            self.store.create_job("parse", a["file_id"], a["paper_id"], "request-1", "work-1", {"different": True})

    def test_cancelled_or_expired_worker_cannot_publish_and_retry_has_new_lease(self):
        a = self.asset()
        job = self.store.create_job("parse", a["file_id"], a["paper_id"])
        claimed = self.store.claim_job()
        self.store.cancel_job(job["id"])
        with self.assertRaises(StoreError) as raised:
            self.parse(a, [self.ref()], job_id=job["id"], lease_token=claimed["lease_token"])
        self.assertEqual(raised.exception.code, "stale_job_lease")
        self.assertEqual(self.count("extraction_runs"), 0)
        self.store.retry_job(job["id"])
        retry = self.store.claim_job()
        self.assertEqual(retry["attempt"], 2)
        self.assertNotEqual(retry["lease_token"], claimed["lease_token"])
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
        self.assertEqual(self.store.recover_jobs(), 1)
        with self.assertRaises(StoreError):
            self.store.update_job(job["id"], lease_token=retry["lease_token"], status="succeeded")
        self.assertEqual(self.store.get_job(job["id"])["status"], "interrupted")

    def test_backup_reopens_with_identical_graph_and_evidence(self):
        a = self.asset()
        self.parse(a, [self.ref()])
        destination = Path(self.temp.name) / "backup.sqlite3"
        result = self.store.backup(destination)
        restored = Store(destination)
        self.assertFalse(result["includes_pdf_files"])
        self.assertEqual(restored.graph(), self.store.graph())
        self.assertEqual(restored.list_references(a["paper_id"]), self.store.list_references(a["paper_id"]))


if __name__ == "__main__":
    unittest.main()
