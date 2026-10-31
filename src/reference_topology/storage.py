"""Transactional local catalogue with immutable extraction and decision evidence.

Every public operation opens its own SQLite connection. PDF bytes are owned by
the file service; this module only persists their identities and registered URIs.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


class StoreError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id() -> str:
    return str(uuid4())


def _fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).casefold().encode()).hexdigest()


_SCALARS = {
    "title", "publication_type", "venue", "volume", "issue", "pages",
    "article_number", "date_text", "abstract", "language", "year",
}
_TERMINAL_JOBS = {"succeeded", "partial", "failed", "cancelled", "interrupted"}


class Store:
    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise

    def _migrate(self) -> None:
        scripts = sorted((Path(__file__).parent / "migrations").glob("[0-9]*.sql"))
        if not scripts:
            raise StoreError("migration_missing", "Packaged SQL migrations are missing.", 500)
        with self.connection() as con:
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise StoreError("wal_unavailable", "SQLite could not enable WAL on the data directory.", 500)
            con.execute("BEGIN IMMEDIATE")
            try:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
                applied = {r["version"]: r for r in con.execute("SELECT * FROM schema_migrations")} if exists else {}
                if applied and max(applied) > self.SCHEMA_VERSION:
                    raise StoreError("schema_too_new", "This database was created by a newer application.", 409)
                for script in scripts:
                    version = int(script.name.split("_", 1)[0])
                    sql = script.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode()).hexdigest()
                    if version in applied:
                        if applied[version]["checksum"] != checksum:
                            raise StoreError("migration_changed", f"Applied migration {version} changed.", 409)
                        continue
                    statement = ""
                    for line in sql.splitlines(keepends=True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            con.execute(statement)
                            statement = ""
                    if statement.strip() and not statement.lstrip().startswith("--"):
                        raise StoreError("migration_invalid", f"Incomplete SQL migration {script.name}.", 500)
                    con.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (version, script.name, checksum, _now()))
                    con.execute(f"PRAGMA user_version={version:d}")
                con.commit()
            except Exception:
                con.rollback()
                raise

    @staticmethod
    def _require(con: sqlite3.Connection, table: str, object_id: str) -> sqlite3.Row:
        # Table names are internal constants, never request values.
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise StoreError("not_found", f"{table} record was not found.", 404)
        return row

    def _canonical(self, con: sqlite3.Connection, paper_id: str) -> str:
        row = self._require(con, "papers", paper_id)
        return row["canonical_paper_id"] or row["id"]

    def canonical_id(self, paper_id: str) -> str:
        with self.connection() as con:
            return self._canonical(con, paper_id)

    @staticmethod
    def _family(con: sqlite3.Connection, root_id: str) -> list[str]:
        return [r[0] for r in con.execute("SELECT id FROM papers WHERE id=? OR canonical_paper_id=?", (root_id, root_id))]

    def _metadata_view(self, con: sqlite3.Connection, root_id: str) -> dict:
        """Select root fields first without moving alias-owned observations.

        A root's explicit null/empty manual selection also takes precedence.
        Missing root fields can be supplied by an alias and retain its identity
        in metadata_sources, making the projection reversible with the merge.
        """
        paper = dict(self._require(con, "papers", root_id))
        selected = {r[0] for r in con.execute("SELECT field_name FROM metadata_claims WHERE paper_id=? AND is_selected=1", (root_id,))}
        paper["metadata_sources"] = {field: root_id for field in _SCALARS if paper.get(field) is not None or field in selected}
        aliases = con.execute("SELECT * FROM papers WHERE canonical_paper_id=? ORDER BY created_at,id", (root_id,)).fetchall()
        for alias in aliases:
            for field in _SCALARS:
                if paper.get(field) is None and field not in selected and alias[field] is not None:
                    paper[field] = alias[field]
                    paper["metadata_sources"][field] = alias["id"]
        author_owner = root_id
        authors = [dict(r) for r in con.execute("SELECT * FROM paper_authors WHERE paper_id=? ORDER BY position", (root_id,))]
        if not authors and "authors" not in selected:
            for alias in aliases:
                authors = [dict(r) for r in con.execute("SELECT * FROM paper_authors WHERE paper_id=? ORDER BY position", (alias["id"],))]
                if authors:
                    author_owner = alias["id"]
                    break
        paper["authors"] = authors
        paper["metadata_sources"]["authors"] = author_owner
        return paper

    @staticmethod
    def _new_paper(con: sqlite3.Connection) -> str:
        pid = _id()
        now = _now()
        con.execute("INSERT INTO papers(id,created_at,updated_at) VALUES(?,?,?)", (pid, now, now))
        return pid

    @staticmethod
    def _normalized_identifiers(metadata: dict) -> list[dict]:
        from .matching import normalize_arxiv, normalize_doi
        values = list(metadata.get("identifiers") or [])
        for scheme in ("doi", "arxiv"):
            if metadata.get(scheme):
                values.append({"scheme": scheme, "value": metadata[scheme]})
        result = {}
        for item in values:
            if not isinstance(item, dict):
                continue
            scheme = str(item.get("scheme") or item.get("type") or "").lower()
            raw = item.get("value_raw") or item.get("value") or item.get("value_normalized")
            normalized = normalize_doi(raw) if scheme == "doi" else normalize_arxiv(raw) if scheme == "arxiv" else None
            if normalized:
                result[(scheme, normalized)] = {"scheme": scheme, "value_raw": str(raw), "value_normalized": normalized}
        return list(result.values())

    def _metadata(self, con: sqlite3.Connection, paper_id: str, fields: dict, *, manual: bool = False,
                  run_id: str | None = None, entry_id: str | None = None) -> bool:
        from .matching import normalize_title
        metadata = {k: v for k, v in fields.items() if k in _SCALARS or k == "authors"}
        if any(k in fields for k in ("identifiers", "doi", "arxiv")):
            identifiers = self._normalized_identifiers(fields)
            if identifiers or manual:
                metadata["identifiers"] = identifiers
        changed = False
        for name, value in metadata.items():
            if not manual and (value is None or value == "" or value == []):
                continue
            if name == "year" and value is not None:
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    raise StoreError("invalid_metadata", "Year must be an integer or null.")
                if value < 1 or value > 9999:
                    raise StoreError("invalid_metadata", "Year must be between 1 and 9999.")
            if name == "authors":
                if value is None:
                    value = []
                if not isinstance(value, list):
                    raise StoreError("invalid_metadata", "Authors must be an ordered list.")
                value = [({"display_name": a} if isinstance(a, str) else dict(a)) for a in value]
                for author in value:
                    author["display_name"] = author.get("display_name") or " ".join(filter(None, [author.get("given_name"), author.get("family_name")]))
                    if not author["display_name"]:
                        raise StoreError("invalid_metadata", "Each author needs a display name.")
            elif name in _SCALARS and name != "year" and value is not None:
                if not isinstance(value, str):
                    raise StoreError("invalid_metadata", f"{name} must be text or null.")
                value = value.strip() or None
            old = con.execute("SELECT * FROM metadata_claims WHERE paper_id=? AND field_name=? AND is_selected=1", (paper_id, name)).fetchone()
            selected = manual or old is None or not old["is_manual"]
            if name == "identifiers":
                for identifier in value:
                    owner = con.execute("SELECT paper_id FROM paper_identifiers WHERE scheme=? AND value_normalized=?", (identifier["scheme"], identifier["value_normalized"])).fetchone()
                    if owner and self._canonical(con, owner[0]) != self._canonical(con, paper_id):
                        if manual:
                            raise StoreError("identifier_conflict", "This identifier belongs to another paper; review or merge the records.", 409)
                        selected = False
            cid = _id()
            if selected and old:
                con.execute("UPDATE metadata_claims SET is_selected=0 WHERE id=?", (old["id"],))
            con.execute("""INSERT INTO metadata_claims
                (id,paper_id,field_name,value_json,source_kind,run_id,reference_entry_id,is_selected,is_manual,supersedes_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, paper_id, name, _json(value), "manual" if manual else "reference" if entry_id else "pdf",
                 run_id, entry_id, int(selected), int(manual), old["id"] if selected and old else None, _now()))
            if not selected:
                continue
            changed = True
            if name in _SCALARS:
                con.execute(f"UPDATE papers SET {name}=? WHERE id=?", (value, paper_id))
                if name == "title":
                    con.execute("UPDATE papers SET title_normalized=? WHERE id=?", (normalize_title(value), paper_id))
            elif name == "authors":
                con.execute("DELETE FROM paper_authors WHERE paper_id=?", (paper_id,))
                for position, author in enumerate(value, 1):
                    con.execute("INSERT INTO paper_authors VALUES(?,?,?,?,?,?)", (paper_id, position, author["display_name"], author.get("given_name"), author.get("family_name"), author.get("orcid")))
            elif name == "identifiers":
                desired = {(x["scheme"], x["value_normalized"]) for x in value}
                for current in con.execute("SELECT * FROM paper_identifiers WHERE paper_id=?", (paper_id,)).fetchall():
                    if (current["scheme"], current["value_normalized"]) not in desired:
                        con.execute("DELETE FROM paper_identifiers WHERE id=?", (current["id"],))
                for identifier in value:
                    con.execute("""INSERT INTO paper_identifiers(id,paper_id,scheme,value_raw,value_normalized,metadata_claim_id)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(scheme,value_normalized) DO NOTHING""",
                        (_id(), paper_id, identifier["scheme"], identifier["value_raw"], identifier["value_normalized"], cid))
        if changed:
            con.execute("""UPDATE papers SET updated_at=?,row_version=row_version+1,
                record_status=CASE WHEN ? THEN 'reviewed' WHEN title IS NOT NULL OR EXISTS
                (SELECT 1 FROM paper_identifiers WHERE paper_id=papers.id) THEN 'identified' ELSE record_status END
                WHERE id=?""", (_now(), int(manual), paper_id))
        return changed

    def create_paper(self, metadata: dict | None = None, *, manual: bool = True) -> dict:
        with self._write() as con:
            pid = self._new_paper(con)
            if metadata:
                self._metadata(con, pid, metadata, manual=manual)
            return self._detail(con, pid)

    def register_asset(self, sha256: str, byte_size: int, canonical_uri: str, original_uri: str | None,
                       display_name: str, host_id: str, root_key: str, location_kind: str) -> dict:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise StoreError("invalid_hash", "A SHA-256 content digest is required.")
        if byte_size < 0 or location_kind not in {"linked", "managed"}:
            raise StoreError("invalid_file", "Invalid file size or import mode.")
        with self._write() as con:
            existing = con.execute("SELECT * FROM pdf_assets WHERE sha256=?", (sha256.lower(),)).fetchone()
            created = existing is None
            if existing:
                aid = existing["id"]
                if existing["byte_size"] != byte_size:
                    raise StoreError("hash_size_conflict", "The existing hash has a different recorded size.", 409)
            else:
                aid = _id()
                con.execute("INSERT INTO pdf_assets(id,sha256,byte_size,created_at) VALUES(?,?,?,?)", (aid, sha256.lower(), byte_size, _now()))
            self._location(con, aid, canonical_uri, original_uri, display_name, host_id, root_key, location_kind, byte_size)
            linked = con.execute("SELECT paper_id FROM paper_files WHERE asset_id=? ORDER BY is_preferred DESC,created_at", (aid,)).fetchone()
            if linked:
                pid = self._canonical(con, linked[0])
            else:
                pid = self._new_paper(con)
                con.execute("INSERT INTO paper_files VALUES(?,?,?,1,?)", (pid, aid, "primary", _now()))
            return {"file_id": aid, "asset_id": aid, "paper_id": pid, "created": created}

    @staticmethod
    def _location(con: sqlite3.Connection, asset_id: str, canonical_uri: str, original_uri: str | None,
                  display_name: str, host_id: str, root_key: str, location_kind: str, byte_size: int) -> None:
        old = con.execute("SELECT * FROM file_locations WHERE host_id=? AND canonical_uri=? AND is_current=1", (host_id, canonical_uri)).fetchone()
        if old and old["asset_id"] == asset_id:
            con.execute("UPDATE file_locations SET status='available',last_checked_at=?,observed_size=? WHERE id=?", (_now(), byte_size, old["id"]))
            return
        if old:
            con.execute("UPDATE file_locations SET status='changed',is_current=0,last_checked_at=? WHERE id=?", (_now(), old["id"]))
        con.execute("""INSERT INTO file_locations(id,asset_id,host_id,canonical_uri,original_uri,display_name,
            root_key,location_kind,observed_size,created_at,last_checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (_id(), asset_id, host_id, canonical_uri, original_uri, display_name, root_key, location_kind, byte_size, _now(), _now()))

    def rebind_asset(self, file_id: str, canonical_uri: str, original_uri: str | None, display_name: str,
                     host_id: str, root_key: str, location_kind: str = "linked", sha256: str | None = None) -> dict:
        with self._write() as con:
            asset = self._require(con, "pdf_assets", file_id)
            if sha256 is not None and sha256.lower() != asset["sha256"]:
                raise StoreError("content_changed", "The selected PDF is not the registered content.", 409)
            self._location(con, file_id, canonical_uri, original_uri, display_name, host_id, root_key, location_kind, asset["byte_size"])
            return self._asset(con, file_id)

    def set_location_status(self, location_id: str, status: str) -> None:
        if status not in {"available", "missing", "inaccessible", "changed"}:
            raise StoreError("invalid_status", "Unknown file location status.")
        with self._write() as con:
            self._require(con, "file_locations", location_id)
            con.execute("UPDATE file_locations SET status=?,last_checked_at=? WHERE id=?", (status, _now(), location_id))

    def _asset(self, con: sqlite3.Connection, file_id: str) -> dict:
        asset = dict(self._require(con, "pdf_assets", file_id))
        asset["file_id"] = file_id
        asset["locations"] = [dict(r) for r in con.execute("SELECT * FROM file_locations WHERE asset_id=? ORDER BY is_current DESC,created_at DESC", (file_id,))]
        asset["paper_ids"] = sorted({self._canonical(con, r[0]) for r in con.execute("SELECT paper_id FROM paper_files WHERE asset_id=?", (file_id,))})
        return asset

    def get_asset(self, file_id: str) -> dict:
        with self.connection() as con:
            return self._asset(con, file_id)

    def _detail(self, con: sqlite3.Connection, paper_id: str) -> dict:
        root_id = self._canonical(con, paper_id)
        paper = self._metadata_view(con, root_id)
        family = self._family(con, root_id)
        placeholders = ",".join("?" for _ in family)
        paper["requested_id"] = paper_id
        paper["aliases"] = [p for p in family if p != root_id]
        paper["identifiers"] = [dict(r) for r in con.execute(f"SELECT * FROM paper_identifiers WHERE paper_id IN ({placeholders}) ORDER BY scheme,value_normalized", family)]
        for item in paper["identifiers"]:
            item["value"] = item["value_normalized"]
            paper.setdefault(item["scheme"], item["value_normalized"])
        file_rows = con.execute(f"""SELECT a.*,pf.role,pf.is_preferred,pf.paper_id AS owner_paper_id
            FROM paper_files pf JOIN pdf_assets a ON a.id=pf.asset_id WHERE pf.paper_id IN ({placeholders})
            ORDER BY (pf.paper_id=?) DESC,pf.is_preferred DESC,pf.created_at,a.id""", (*family, root_id)).fetchall()
        paper["files"] = []
        seen = set()
        for row in file_rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            item = dict(row)
            item["file_id"] = row["id"]
            item["locations"] = [dict(r) for r in con.execute("SELECT * FROM file_locations WHERE asset_id=? ORDER BY is_current DESC,created_at DESC", (row["id"],))]
            item["available"] = any(x["is_current"] and x["status"] == "available" for x in item["locations"])
            paper["files"].append(item)
        preferred = next((f for f in paper["files"] if f["available"]), None)
        paper["has_pdf"] = preferred is not None
        paper["file_id"] = preferred["id"] if preferred else None
        paper["display_title"] = paper["title"] or next((l["display_name"] for f in paper["files"] for l in f["locations"]), "Untitled paper")
        paper["claims"] = []
        for row in con.execute(f"SELECT * FROM metadata_claims WHERE paper_id IN ({placeholders}) ORDER BY created_at DESC", family):
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            paper["claims"].append(item)
        paper["runs"] = [dict(r) for r in con.execute(f"SELECT * FROM extraction_runs WHERE source_paper_id IN ({placeholders}) ORDER BY started_at DESC", family)]
        paper["reference_count"] = con.execute(f"SELECT COUNT(*) FROM reference_entries e JOIN extraction_runs r ON r.id=e.run_id WHERE e.source_paper_id IN ({placeholders}) AND r.is_active=1", family).fetchone()[0]
        paper["outgoing_count"] = con.execute("SELECT COUNT(*) FROM citation_edges WHERE citing_paper_id=?", (root_id,)).fetchone()[0]
        paper["incoming_count"] = con.execute("SELECT COUNT(*) FROM citation_edges WHERE cited_paper_id=?", (root_id,)).fetchone()[0]
        return paper

    def paper_detail(self, paper_id: str) -> dict:
        with self.connection() as con:
            return self._detail(con, paper_id)

    def _all_papers(self, con: sqlite3.Connection) -> list[dict]:
        result = []
        for row in con.execute("SELECT * FROM papers WHERE canonical_paper_id IS NULL AND archived_at IS NULL ORDER BY created_at,id").fetchall():
            item = self._metadata_view(con, row["id"])
            family = self._family(con, row["id"])
            marks = ",".join("?" for _ in family)
            item["identifiers"] = [dict(r) for r in con.execute(f"SELECT * FROM paper_identifiers WHERE paper_id IN ({marks})", family)]
            for identifier in item["identifiers"]:
                identifier["value"] = identifier["value_normalized"]
                item.setdefault(identifier["scheme"], identifier["value_normalized"])
            result.append(item)
        return result

    def list_papers(self, q: str = "", limit: int = 100, offset: int = 0) -> dict:
        limit, offset = max(1, min(int(limit), 1000)), max(0, int(offset))
        with self.connection() as con:
            query = "papers.canonical_paper_id IS NULL AND papers.archived_at IS NULL"
            params: list = []
            if q.strip():
                query += """ AND (EXISTS (SELECT 1 FROM papers alias WHERE
                    COALESCE(alias.canonical_paper_id,alias.id)=papers.id AND alias.title LIKE ?)
                    OR CAST(papers.year AS TEXT) LIKE ? OR EXISTS
                    (SELECT 1 FROM papers alias JOIN paper_authors a ON a.paper_id=alias.id
                     WHERE COALESCE(alias.canonical_paper_id,alias.id)=papers.id AND a.display_name LIKE ?) OR EXISTS
                    (SELECT 1 FROM papers alias JOIN paper_identifiers i ON i.paper_id=alias.id
                     WHERE COALESCE(alias.canonical_paper_id,alias.id)=papers.id AND i.value_normalized LIKE ?))"""
                params = [f"%{q.strip()}%"] * 4
            total = con.execute(f"SELECT COUNT(*) FROM papers WHERE {query}", params).fetchone()[0]
            ids = con.execute(f"SELECT id FROM papers WHERE {query} ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            return {"items": [self._detail(con, r[0]) for r in ids], "total": total, "limit": limit, "offset": offset}

    def edit_paper(self, paper_id: str, fields: dict, expected_version: int | None = None) -> dict:
        unsupported = set(fields) - (_SCALARS | {"authors", "identifiers", "doi", "arxiv"})
        if unsupported:
            raise StoreError("invalid_metadata", "Unsupported metadata field: " + sorted(unsupported)[0])
        with self._write() as con:
            root = self._canonical(con, paper_id)
            row = self._require(con, "papers", root)
            if expected_version is not None and row["row_version"] != expected_version:
                raise StoreError("revision_conflict", "This paper changed after it was opened. Reload before saving.", 409)
            self._metadata(con, root, fields, manual=True)
            return self._detail(con, root)

    def _merge(self, con: sqlite3.Connection, source: str, target: str, reason: str, actor: str) -> dict:
        source, target = self._canonical(con, source), self._canonical(con, target)
        if source == target:
            raise StoreError("already_merged", "These records already have the same canonical identity.", 409)
        affected = self._family(con, source) + [target]
        before = {pid: {"canonical_paper_id": self._require(con, "papers", pid)["canonical_paper_id"],
                        "row_version": self._require(con, "papers", pid)["row_version"]} for pid in affected}
        for pid in affected:
            if pid != target:
                con.execute("UPDATE papers SET canonical_paper_id=?,row_version=row_version+1,updated_at=? WHERE id=?", (target, _now(), pid))
            else:
                con.execute("UPDATE papers SET row_version=row_version+1,updated_at=? WHERE id=?", (_now(), pid))
        after = {pid: {"canonical_paper_id": self._require(con, "papers", pid)["canonical_paper_id"],
                       "row_version": self._require(con, "papers", pid)["row_version"]} for pid in affected}
        event_id = _id()
        con.execute("INSERT INTO paper_merge_events VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (event_id, source, target, reason, actor, _json(before), _json(after),
                     _json({pid: value["row_version"] for pid, value in after.items()}), _now()))
        return {"id": event_id, "merge_id": event_id, "from_paper_id": source, "to_paper_id": target, "paper_id": target}

    def merge_papers(self, from_paper_id: str, to_paper_id: str, reason: str = "User confirmed duplicate papers",
                     expected_versions: dict | None = None) -> dict:
        with self._write() as con:
            if expected_versions:
                for pid, version in expected_versions.items():
                    if self._require(con, "papers", pid)["row_version"] != version:
                        raise StoreError("revision_conflict", "A record changed before the merge.", 409)
            result = self._merge(con, from_paper_id, to_paper_id, reason, "user")
            result["paper"] = self._detail(con, result["paper_id"])
            return result

    def revert_merge(self, merge_id: str) -> dict:
        with self._write() as con:
            event = self._require(con, "paper_merge_events", merge_id)
            if event["reverted_at"]:
                raise StoreError("already_reverted", "This merge was already reverted.", 409)
            before, after = json.loads(event["before_json"]), json.loads(event["after_json"])
            for pid, expected in after.items():
                row = self._require(con, "papers", pid)
                if row["row_version"] != expected["row_version"] or row["canonical_paper_id"] != expected["canonical_paper_id"]:
                    raise StoreError("revision_conflict", "A merged record has later edits or merges; review before undoing.", 409)
            for pid, old in before.items():
                con.execute("UPDATE papers SET canonical_paper_id=?,row_version=row_version+1,updated_at=? WHERE id=?", (old["canonical_paper_id"], _now(), pid))
            con.execute("UPDATE paper_merge_events SET reverted_at=? WHERE id=?", (_now(), merge_id))
            return {"id": merge_id, "reverted": True, "paper_ids": list(before)}

    def _source_for_asset(self, con: sqlite3.Connection, file_id: str, paper_id: str) -> str:
        root = self._canonical(con, paper_id)
        row = con.execute("""SELECT pf.paper_id FROM paper_files pf JOIN papers p ON p.id=pf.paper_id
            WHERE pf.asset_id=? AND COALESCE(p.canonical_paper_id,p.id)=?
            ORDER BY pf.created_at,pf.paper_id LIMIT 1""", (file_id, root)).fetchone()
        if row is None:
            raise StoreError("file_paper_mismatch", "This PDF is not attached to the specified paper.", 409)
        return row[0]

    def _assert(self, con: sqlite3.Connection, *, source: str, target: str | None, entry_id: str | None,
                status: str, method: str, actor: str = "system", score: float | None = None,
                evidence: dict | None = None, supersedes: str | None = None, pinned: bool = False,
                review_state: str = "current") -> str:
        aid = _id()
        con.execute("""INSERT INTO citation_assertions(id,citing_paper_id,cited_paper_id,reference_entry_id,
            source_kind,status,review_state,is_pinned,method,evidence_json,score,actor,supersedes_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, source, target, entry_id, "pdf" if entry_id else "manual", status, review_state,
             int(pinned), method, _json(evidence or {}), score, actor, supersedes, _now()))
        return aid

    def publish_parse(self, file_id: str, paper_id: str, result: dict, config_hash: str = "",
                      output_uri: str | None = None, *, job_id: str | None = None,
                      lease_token: str | None = None, base_run_id: str | None = None) -> dict:
        status = result.get("status", "succeeded")
        if status not in {"succeeded", "partial", "needs_ocr", "failed", "cancelled"}:
            raise StoreError("invalid_parse_result", "Unknown parser result status.")
        references = result.get("references") or []
        metadata = result.get("metadata") or {}
        if not isinstance(references, list) or not isinstance(metadata, dict):
            raise StoreError("invalid_parse_result", "Parser metadata and references have invalid types.")
        prepared = []
        ordinals = set()
        for position, reference in enumerate(references, 1):
            raw = str(reference.get("raw_text") or "").strip()
            ordinal = int(reference.get("ordinal", position))
            if not raw or ordinal < 1 or ordinal in ordinals:
                raise StoreError("invalid_parse_result", "Reference entries need nonempty text and unique positive ordinals.")
            ordinals.add(ordinal)
            prepared.append({**reference, "raw_text": raw, "ordinal": ordinal,
                             "fingerprint": reference.get("fingerprint") or _fingerprint(raw)})
        engine = str(result.get("engine") or "python-rules")
        version = str(result.get("engine_version") or "1")
        with self._write() as con:
            asset = self._require(con, "pdf_assets", file_id)
            source = self._source_for_asset(con, file_id, paper_id)
            if job_id:
                job = self._validate_lease(con, job_id, lease_token)
                if job["asset_id"] != file_id:
                    raise StoreError("job_input_mismatch", "The job lease belongs to another PDF.", 409)
            previous = con.execute("SELECT * FROM extraction_runs WHERE source_paper_id=? AND asset_id=? AND is_active=1", (source, file_id)).fetchone()
            if base_run_id:
                if not previous or previous["id"] != base_run_id:
                    raise StoreError("revision_conflict", "The reference snapshot changed before the revision.", 409)
            root = self._canonical(con, source)
            auto_merge_target = None
            # A newly registered PDF can complete an existing reference-only node.
            # Existing curated papers are never silently merged by a parse.
            if metadata and self._require(con, "papers", root)["record_status"] == "stub":
                from .matching import match_reference
                candidates = match_reference(metadata, [p for p in self._all_papers(con) if p["id"] != root])
                safe = [c for c in candidates if c.get("safe")]
                manual = con.execute("SELECT 1 FROM metadata_claims WHERE paper_id=? AND is_manual=1 LIMIT 1", (root,)).fetchone()
                if len(safe) == 1 and not manual:
                    auto_merge_target = safe[0]["paper_id"]
            run_id = _id()
            warnings = list(result.get("warnings") or [])
            active = status in {"succeeded", "partial"}
            if previous and previous["parser_name"] == "manual-revision" and engine != "manual-revision":
                active = False
                warnings.append("manual_revision_preserved")
            if previous and previous["status"] == "succeeded" and status != "succeeded":
                active = False
                warnings.append("previous_complete_snapshot_preserved")
            if not active and previous is None and status not in {"succeeded", "partial"}:
                warnings.append("no_reference_snapshot_published")
            cache_key = hashlib.sha256(_json([asset["sha256"], engine, version, config_hash]).encode()).hexdigest()
            if active and previous:
                con.execute("UPDATE extraction_runs SET is_active=0 WHERE id=?", (previous["id"],))
            con.execute("""INSERT INTO extraction_runs(id,asset_id,source_paper_id,parser_name,parser_version,
                config_hash,input_sha256,cache_key,status,is_active,base_run_id,output_uri,output_sha256,
                warning_json,error_code,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, file_id, source, engine, version, config_hash, asset["sha256"], cache_key, status,
                 int(active), base_run_id, output_uri, result.get("output_sha256"), _json(warnings), result.get("error_code"), _now(), _now()))
            old_entries = con.execute("SELECT * FROM reference_entries WHERE run_id=?", (previous["id"],)).fetchall() if previous else []
            old_by_fp: dict[str, list] = {}
            for entry in old_entries:
                old_by_fp.setdefault(entry["text_fingerprint"], []).append(entry)
            new_counts = Counter(e["fingerprint"] for e in prepared)
            for reference in prepared:
                fingerprint = reference["fingerprint"]
                old = old_by_fp.get(fingerprint, [])
                aligned = old[0] if len(old) == 1 and new_counts[fingerprint] == 1 and previous and previous["input_sha256"] == asset["sha256"] else None
                entry_id = _id()
                con.execute("""INSERT INTO reference_entries(id,run_id,source_paper_id,ordinal,label,raw_text,
                    parsed_json,locations_json,text_fingerprint,aligned_from_entry_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (entry_id, run_id, source, reference["ordinal"], reference.get("label"), reference["raw_text"],
                     _json(reference.get("parsed") or {}), _json(reference.get("locations")) if reference.get("locations") is not None else None,
                     fingerprint, aligned["id"] if aligned else None, _now()))
                if active and aligned:
                    for decision in con.execute("SELECT * FROM citation_assertions WHERE reference_entry_id=? AND is_current=1 AND is_pinned=1", (aligned["id"],)).fetchall():
                        con.execute("UPDATE citation_assertions SET is_current=0 WHERE id=?", (decision["id"],))
                        self._assert(con, source=source, target=decision["cited_paper_id"], entry_id=entry_id,
                                     status=decision["status"], method="inherited-manual", actor="user", score=decision["score"],
                                     evidence={"aligned_from_entry_id": aligned["id"], "previous_decision": decision["id"]},
                                     supersedes=decision["id"], pinned=True)
            if active and previous:
                con.execute("""UPDATE citation_assertions SET review_state='needs_review'
                    WHERE is_current=1 AND is_pinned=1 AND reference_entry_id IN
                    (SELECT id FROM reference_entries WHERE run_id=?)""", (previous["id"],))
            if result.get("page_count") is not None:
                con.execute("UPDATE pdf_assets SET page_count=? WHERE id=?", (int(result["page_count"]), file_id))
            if metadata and status not in {"failed", "cancelled"}:
                self._metadata(con, source, metadata, run_id=run_id)
            if auto_merge_target and status not in {"failed", "cancelled"}:
                root = self._merge(con, root, auto_merge_target, "PDF metadata matched an existing strong identifier", "system")["paper_id"]
            matching = self._match_pending(con) if active else {"accepted": 0, "candidates": 0, "created_papers": 0}
            published = {"run_id": run_id, "paper_id": root, "file_id": file_id, "reference_count": len(prepared),
                         "active": active, "status": status, "warnings": warnings, "matching": matching}
            if job_id:
                # The lease was checked under this write transaction. Commit the
                # snapshot and terminal state together so cancel/recovery cannot
                # report an already published result as cancelled/interrupted.
                terminal = "partial" if status == "needs_ocr" else status
                finished = _now()
                history = json.loads(job["attempt_history_json"])
                history.append({"attempt": job["attempt"], "status": terminal, "finished_at": finished,
                                "error_code": result.get("error_code"), "error_message": result.get("error_message")})
                con.execute("""UPDATE jobs SET status=?,progress_current=3,progress_total=3,lease_until=NULL,
                    heartbeat_at=?,updated_at=?,error_code=?,error_message=?,result_json=?,attempt_history_json=? WHERE id=?""",
                    (terminal, finished, finished, result.get("error_code"), result.get("error_message"),
                     _json({**published, "parse_status": status}), _json(history), job_id))
            return published

    def revise_references(self, paper_id: str, base_run_id: str, references: list[dict]) -> dict:
        with self.connection() as con:
            run = dict(self._require(con, "extraction_runs", base_run_id))
            if self._canonical(con, run["source_paper_id"]) != self._canonical(con, paper_id):
                raise StoreError("revision_conflict", "The snapshot belongs to another paper.", 409)
        return self.publish_parse(run["asset_id"], paper_id,
                                  {"engine": "manual-revision", "engine_version": "1", "status": "succeeded",
                                   "metadata": {}, "references": references, "warnings": []}, base_run_id=base_run_id)

    def _match_pending(self, con: sqlite3.Connection) -> dict:
        from .matching import match_reference
        papers = self._all_papers(con)
        stats = {"accepted": 0, "candidates": 0, "created_papers": 0, "unresolved": 0}
        entries = con.execute("""SELECT e.* FROM reference_entries e JOIN extraction_runs r ON r.id=e.run_id
            WHERE r.is_active=1 ORDER BY e.created_at,e.ordinal""").fetchall()
        for entry in entries:
            current = con.execute("SELECT * FROM citation_assertions WHERE reference_entry_id=? AND is_current=1", (entry["id"],)).fetchall()
            if any(r["status"] == "accepted" for r in current):
                continue
            if any(r["is_pinned"] and r["status"] in {"rejected", "retracted"} and r["cited_paper_id"] is None for r in current):
                continue
            blocked = {self._canonical(con, r["cited_paper_id"]) for r in current
                       if r["is_pinned"] and r["cited_paper_id"] and r["status"] in {"rejected", "retracted"}}
            source = self._canonical(con, entry["source_paper_id"])
            parsed = json.loads(entry["parsed_json"])
            candidates = match_reference(parsed, papers)
            candidates = [c for c in candidates if c["paper_id"] not in blocked]
            identifiers = self._normalized_identifiers(parsed)
            if not candidates and identifiers and not blocked:
                owners = []
                for ident in identifiers:
                    owner = con.execute("SELECT paper_id FROM paper_identifiers WHERE scheme=? AND value_normalized=?", (ident["scheme"], ident["value_normalized"])).fetchone()
                    if owner:
                        owners.append(self._canonical(con, owner[0]))
                if not owners:
                    target = self._new_paper(con)
                    self._metadata(con, target, parsed, entry_id=entry["id"])
                    papers = self._all_papers(con)
                    candidates = [{"paper_id": target, "safe": not parsed.get("_needs_review", False),
                                   "method": "new-strong-identifier", "score": 1.0,
                                   "reason": "A new bibliographic node was registered from an explicit identifier."}]
                    stats["created_papers"] += 1
            safe = [c for c in candidates if c.get("safe") and c["paper_id"] != source]
            winner = safe[0]["paper_id"] if len(safe) == 1 else None
            if not candidates:
                stats["unresolved"] += 1
            for candidate in candidates:
                target = candidate["paper_id"]
                status = "accepted" if winner == target else "candidate"
                if any(r["cited_paper_id"] == target and r["status"] == status and r["actor"] == "system" for r in current):
                    continue
                old = next((r for r in current if r["cited_paper_id"] == target and r["actor"] == "system"), None)
                if old:
                    con.execute("UPDATE citation_assertions SET is_current=0 WHERE id=?", (old["id"],))
                if status == "accepted":
                    con.execute("UPDATE citation_assertions SET is_current=0 WHERE reference_entry_id=? AND actor='system' AND is_current=1", (entry["id"],))
                self._assert(con, source=entry["source_paper_id"], target=target, entry_id=entry["id"],
                             status=status, method=candidate.get("method", "candidate"), score=candidate.get("score"),
                             evidence={"reason": candidate.get("reason", ""), "safe": bool(candidate.get("safe"))},
                             supersedes=old["id"] if old else None)
                stats["accepted" if status == "accepted" else "candidates"] += 1
        return stats

    def match_pending(self) -> dict:
        with self._write() as con:
            return self._match_pending(con)

    def _reference(self, con: sqlite3.Connection, entry: sqlite3.Row) -> dict:
        item = dict(entry)
        item["parsed"] = json.loads(item.pop("parsed_json"))
        item["locations"] = json.loads(item.pop("locations_json")) if item["locations_json"] else []
        item.pop("locations_json", None)
        run = self._require(con, "extraction_runs", entry["run_id"])
        item["active"] = bool(run["is_active"])
        item["assertions"] = []
        for row in con.execute("SELECT * FROM citation_assertions WHERE reference_entry_id=? AND is_current=1 ORDER BY created_at DESC", (entry["id"],)):
            assertion = dict(row)
            assertion["evidence"] = json.loads(assertion.pop("evidence_json"))
            if row["cited_paper_id"]:
                target = self._canonical(con, row["cited_paper_id"])
                paper = self._metadata_view(con, target)
                assertion["target"] = {"id": target, "title": paper["title"], "year": paper["year"]}
                assertion["paper_id"] = target
                # This is a derived review condition: preserving the original
                # decision lets undoing the merge restore its validity exactly.
                if target == self._canonical(con, entry["source_paper_id"]):
                    assertion["review_state"] = "needs_review"
                    assertion["review_reason"] = "canonical_self_citation"
            item["assertions"].append(assertion)
        accepted = next((a for a in item["assertions"] if a["status"] == "accepted" and a["review_state"] == "current" and (run["is_active"] or a["is_pinned"])), None)
        item["matched"] = accepted is not None
        item["target"] = accepted.get("target") if accepted else None
        item["candidates"] = [a for a in item["assertions"] if a["status"] == "candidate"]
        item["status"] = "accepted" if accepted else "needs_review" if any(a["review_state"] == "needs_review" for a in item["assertions"]) else "rejected" if any(a["is_pinned"] and a["status"] == "rejected" for a in item["assertions"]) else "candidate" if item["candidates"] else "unresolved"
        return item

    def list_references(self, paper_id: str) -> list[dict]:
        with self.connection() as con:
            family = self._family(con, self._canonical(con, paper_id))
            marks = ",".join("?" for _ in family)
            rows = con.execute(f"""SELECT e.* FROM reference_entries e JOIN extraction_runs r ON r.id=e.run_id
                WHERE e.source_paper_id IN ({marks}) AND (r.is_active=1 OR EXISTS
                (SELECT 1 FROM citation_assertions a WHERE a.reference_entry_id=e.id AND a.is_current=1
                 AND a.is_pinned=1 AND a.review_state='needs_review'))
                ORDER BY r.started_at DESC,e.ordinal""", family).fetchall()
            return [self._reference(con, row) for row in rows]

    def decide_reference(self, entry_id: str, action: str, target_paper_id: str | None = None,
                         metadata: dict | None = None, *, expected_assertion_id: str | None = None,
                         reason: str = "User reviewed the reference") -> dict:
        action = {"accepted": "accept", "rejected": "reject", "retracted": "retract", "create": "accept", "create-paper": "accept"}.get(action, action)
        if action not in {"accept", "reject", "retract"}:
            raise StoreError("invalid_decision", "Use accept, reject, or retract.")
        with self._write() as con:
            entry = self._require(con, "reference_entries", entry_id)
            run = self._require(con, "extraction_runs", entry["run_id"])
            if not run["is_active"]:
                raise StoreError("revision_conflict", "This reference belongs to an older snapshot. Review the current version.", 409)
            current = con.execute("SELECT * FROM citation_assertions WHERE reference_entry_id=? AND is_current=1", (entry_id,)).fetchall()
            if expected_assertion_id and not any(r["id"] == expected_assertion_id for r in current):
                raise StoreError("revision_conflict", "The reference decision has changed.", 409)
            if metadata and not target_paper_id:
                target_paper_id = self._new_paper(con)
                self._metadata(con, target_paper_id, metadata, manual=True, entry_id=entry_id)
            if target_paper_id:
                target_paper_id = self._canonical(con, target_paper_id)
            if action == "accept" and not target_paper_id:
                raise StoreError("target_required", "Choose a paper or supply bibliographic metadata before accepting.")
            if target_paper_id == self._canonical(con, entry["source_paper_id"]):
                raise StoreError("self_citation", "A paper cannot be matched to its own reference entry; check the identity.", 409)
            selected = current if action == "accept" or target_paper_id is None else [r for r in current if r["cited_paper_id"] and self._canonical(con, r["cited_paper_id"]) == target_paper_id]
            old = next((r for r in selected if r["is_pinned"]), selected[0] if selected else None)
            for row in selected:
                con.execute("UPDATE citation_assertions SET is_current=0 WHERE id=?", (row["id"],))
            self._assert(con, source=entry["source_paper_id"], target=target_paper_id, entry_id=entry_id,
                         status={"accept": "accepted", "reject": "rejected", "retract": "retracted"}[action],
                         method="manual", actor="user", pinned=True, evidence={"reason": reason}, supersedes=old["id"] if old else None)
            root = self._canonical(con, entry["source_paper_id"])
            con.execute("UPDATE papers SET row_version=row_version+1,updated_at=? WHERE id=?", (_now(), root))
            return self._reference(con, entry)

    def _node(self, con: sqlite3.Connection, paper_id: str) -> dict:
        root = self._canonical(con, paper_id)
        paper = self._metadata_view(con, root)
        file = con.execute("""SELECT a.id,l.display_name FROM papers p JOIN paper_files pf ON pf.paper_id=p.id
            JOIN pdf_assets a ON a.id=pf.asset_id JOIN file_locations l ON l.asset_id=a.id
            WHERE COALESCE(p.canonical_paper_id,p.id)=? AND l.is_current=1 AND l.status='available'
            ORDER BY (p.id=?) DESC,pf.is_preferred DESC,pf.created_at,l.created_at LIMIT 1""", (root, root)).fetchone()
        return {"id": root, "type": "paper", "title": paper["title"] or (file["display_name"] if file else "Untitled paper"),
                "year": paper["year"], "has_pdf": file is not None, "file_id": file["id"] if file else None,
                "record_status": paper["record_status"]}

    def graph(self, center: str | None = None, direction: str = "both", depth: int = 1,
              limit: int = 300, max_edges: int = 1000, include_pending: bool = False) -> dict:
        if direction not in {"both", "in", "out", "incoming", "outgoing"}:
            raise StoreError("invalid_direction", "Graph direction must be in, out, or both.")
        direction = {"incoming": "in", "outgoing": "out"}.get(direction, direction)
        depth, limit, max_edges = max(0, min(int(depth), 3)), max(1, min(int(limit), 300)), max(1, min(int(max_edges), 1000))
        with self.connection() as con:
            truncated = False
            omitted_nodes = 0
            omitted_node_ids = set()
            if center:
                center = self._canonical(con, center)
                selected = {center}
                frontier = {center}
                for _ in range(depth):
                    if not frontier:
                        break
                    marks = ",".join("?" for _ in frontier)
                    clause = f"citing_paper_id IN ({marks})" if direction == "out" else f"cited_paper_id IN ({marks})" if direction == "in" else f"citing_paper_id IN ({marks}) OR cited_paper_id IN ({marks})"
                    params = tuple(sorted(frontier)) * (2 if direction == "both" else 1)
                    neighbors = con.execute(f"SELECT * FROM citation_edges WHERE {clause} ORDER BY citing_paper_id,cited_paper_id LIMIT ?", (*params, max_edges + limit + 1)).fetchall()
                    if len(neighbors) > max_edges + limit:
                        truncated = True
                    next_frontier = set()
                    for edge in neighbors:
                        for pid in (edge["citing_paper_id"], edge["cited_paper_id"]):
                            if pid not in selected:
                                if len(selected) < limit:
                                    selected.add(pid)
                                    next_frontier.add(pid)
                                else:
                                    truncated = True
                                    omitted_node_ids.add(pid)
                    frontier = next_frontier
                omitted_nodes = len(omitted_node_ids)
            else:
                rows = con.execute("SELECT id FROM papers WHERE canonical_paper_id IS NULL AND archived_at IS NULL ORDER BY updated_at DESC,id LIMIT ?", (limit + 1,)).fetchall()
                selected = {r[0] for r in rows[:limit]}
                truncated = len(rows) > limit
                omitted_nodes = max(0, con.execute("SELECT COUNT(*) FROM papers WHERE canonical_paper_id IS NULL AND archived_at IS NULL").fetchone()[0] - len(selected))
            nodes = [self._node(con, pid) for pid in sorted(selected)]
            edges: list[dict] = []
            omitted_edges = 0
            if selected:
                marks = ",".join("?" for _ in selected)
                params = tuple(sorted(selected)) * 2
                rows = con.execute(f"SELECT * FROM citation_edges WHERE citing_paper_id IN ({marks}) AND cited_paper_id IN ({marks}) ORDER BY citing_paper_id,cited_paper_id LIMIT ?", (*params, max_edges + 1)).fetchall()
                if len(rows) > max_edges:
                    truncated = True
                    omitted_edges = 1
                for row in rows[:max_edges]:
                    edges.append({"id": f"{row['citing_paper_id']}->{row['cited_paper_id']}",
                                  "source": row["citing_paper_id"], "target": row["cited_paper_id"],
                                  "evidence_count": row["evidence_count"], "type": "citation", "pending": False,
                                  "has_local_evidence": bool(row["has_local_evidence"]), "has_external_evidence": bool(row["has_external_evidence"])})
                if include_pending:
                    pending = con.execute(f"""SELECT e.*,COALESCE(p.canonical_paper_id,p.id) AS canonical_source
                        FROM reference_entries e JOIN extraction_runs r ON r.id=e.run_id
                        JOIN papers p ON p.id=e.source_paper_id WHERE r.is_active=1
                        AND COALESCE(p.canonical_paper_id,p.id) IN ({marks}) AND NOT EXISTS
                        (SELECT 1 FROM effective_citation_assertions a WHERE a.reference_entry_id=e.id)
                        ORDER BY e.created_at,e.ordinal LIMIT ?""", (*sorted(selected), limit + 1)).fetchall()
                    for entry in pending:
                        if len(nodes) >= limit or len(edges) >= max_edges:
                            truncated = True
                            omitted_nodes += 1
                            continue
                        parsed = json.loads(entry["parsed_json"])
                        nid = "reference:" + entry["id"]
                        nodes.append({"id": nid, "type": "reference", "title": parsed.get("title") or entry["raw_text"][:120],
                                      "year": parsed.get("year"), "has_pdf": False, "file_id": None, "reference_id": entry["id"], "pending": True})
                        edges.append({"id": "pending:" + entry["id"], "source": entry["canonical_source"], "target": nid,
                                      "evidence_count": 0, "type": "pending", "pending": True})
            return {"nodes": nodes, "edges": edges, "center": center, "direction": direction, "depth": depth,
                    "truncated": truncated, "omitted_nodes": omitted_nodes, "omitted_edges": omitted_edges,
                    "omitted_counts_are_lower_bounds": truncated,
                    "scope": "local", "paper_count": len(selected), "citation_count": sum(not e["pending"] for e in edges),
                    "pending_count": sum(n["type"] == "reference" for n in nodes)}

    def list_citations(self, paper_id: str) -> list[dict]:
        with self.connection() as con:
            root = self._canonical(con, paper_id)
            return [{**self._node(con, row["citing_paper_id"]), **dict(row), "scope": "local"}
                    for row in con.execute("SELECT * FROM citation_edges WHERE cited_paper_id=? AND has_local_evidence=1 ORDER BY citing_paper_id", (root,)).fetchall()]

    def edge_evidence(self, source: str, target: str) -> list[dict]:
        with self.connection() as con:
            source, target = self._canonical(con, source), self._canonical(con, target)
            rows = con.execute("""SELECT a.*,e.raw_text,e.label,e.ordinal,e.locations_json,
                r.id AS run_id,r.parser_name,r.parser_version,r.input_sha256,r.asset_id
                FROM effective_citation_assertions v JOIN citation_assertions a ON a.id=v.id
                LEFT JOIN reference_entries e ON e.id=a.reference_entry_id
                LEFT JOIN extraction_runs r ON r.id=e.run_id
                WHERE v.citing_paper_id=? AND v.cited_paper_id=? ORDER BY a.created_at""", (source, target)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                item["locations"] = json.loads(item.pop("locations_json")) if item["locations_json"] else []
                item.pop("locations_json", None)
                result.append(item)
            return result

    @staticmethod
    def _job_dict(row: sqlite3.Row | dict) -> dict:
        value = dict(row)
        value["input_data"] = json.loads(value.pop("input_json"))
        value["input"] = value["input_data"]
        value["result"] = json.loads(value.pop("result_json")) if value["result_json"] else None
        value.pop("result_json", None)
        value["attempt_history"] = json.loads(value.pop("attempt_history_json"))
        value["request_aliases"] = json.loads(value.pop("request_aliases_json"))
        value["job_id"] = value["id"]
        return value

    def create_job(self, kind: str, asset_id: str | None = None, paper_id: str | None = None,
                   request_key: str | None = None, work_key: str | None = None,
                   input_data: dict | None = None) -> dict:
        request_key = request_key or _id()
        payload = _json(input_data or {})
        work_key = work_key or hashlib.sha256(_json([kind, asset_id, paper_id, input_data or {}]).encode()).hexdigest()
        with self._write() as con:
            existing = con.execute("""SELECT * FROM jobs WHERE request_key=? OR EXISTS
                (SELECT 1 FROM json_each(request_aliases_json) WHERE value=?) LIMIT 1""", (request_key, request_key)).fetchone()
            if existing:
                if (existing["kind"], existing["asset_id"], existing["paper_id"], existing["input_json"]) != (kind, asset_id, paper_id, payload):
                    raise StoreError("request_key_conflict", "The request key was already used for a different operation.", 409)
                return {**self._job_dict(existing), "created": False}
            active = con.execute("SELECT * FROM jobs WHERE work_key=? AND status IN ('queued','running')", (work_key,)).fetchone()
            if active:
                if (active["kind"], active["asset_id"], active["input_json"]) != (kind, asset_id, payload):
                    raise StoreError("work_key_conflict", "The work key belongs to a different operation.", 409)
                aliases = json.loads(active["request_aliases_json"])
                aliases.append(request_key)
                con.execute("UPDATE jobs SET request_aliases_json=? WHERE id=?", (_json(aliases), active["id"]))
                return {**self._job_dict(self._require(con, "jobs", active["id"])), "created": False}
            jid = _id()
            con.execute("""INSERT INTO jobs(id,kind,asset_id,paper_id,request_key,work_key,input_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (jid, kind, asset_id, paper_id, request_key, work_key, payload, _now(), _now()))
            return {**self._job_dict(self._require(con, "jobs", jid)), "created": True}

    def _validate_lease(self, con: sqlite3.Connection, job_id: str, lease_token: str | None) -> sqlite3.Row:
        job = self._require(con, "jobs", job_id)
        if job["status"] != "running" or not lease_token or job["lease_token"] != lease_token or not job["lease_until"] or job["lease_until"] <= _now():
            raise StoreError("stale_job_lease", "The job was cancelled, interrupted, or claimed by another worker.", 409)
        return job

    def claim_job(self, lease_seconds: int = 120) -> dict | None:
        with self._write() as con:
            row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="microseconds")
            con.execute("""UPDATE jobs SET status='running',attempt=attempt+1,lease_token=?,lease_until=?,
                heartbeat_at=?,updated_at=?,error_code=NULL,error_message=NULL WHERE id=?""", (_id(), until, _now(), _now(), row["id"]))
            return self._job_dict(self._require(con, "jobs", row["id"]))

    def update_job(self, job_id: str, *, lease_token: str | None = None, status: str | None = None,
                   progress_current: int | None = None, progress_total: int | None = None,
                   error_code: str | None = None, error_message: str | None = None,
                   result: Any = None, lease_seconds: int = 120) -> dict:
        if status is not None and status not in _TERMINAL_JOBS | {"running"}:
            raise StoreError("invalid_job_status", "Workers may update running jobs or finish an attempt.")
        with self._write() as con:
            row = self._require(con, "jobs", job_id)
            if row["status"] in _TERMINAL_JOBS and row["status"] == status and lease_token and row["lease_token"] == lease_token:
                return self._job_dict(row)
            self._validate_lease(con, job_id, lease_token)
            new_status = status or "running"
            current = row["progress_current"] if progress_current is None else int(progress_current)
            total = row["progress_total"] if progress_total is None else int(progress_total)
            if current < 0 or total < 0:
                raise StoreError("invalid_progress", "Job progress cannot be negative.")
            until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="microseconds") if new_status == "running" else None
            history = json.loads(row["attempt_history_json"])
            if new_status in _TERMINAL_JOBS:
                history.append({"attempt": row["attempt"], "status": new_status, "finished_at": _now(), "error_code": error_code, "error_message": error_message})
            con.execute("""UPDATE jobs SET status=?,progress_current=?,progress_total=?,lease_until=?,heartbeat_at=?,
                updated_at=?,error_code=?,error_message=?,result_json=?,attempt_history_json=? WHERE id=?""",
                (new_status, current, total, until, _now(), _now(), error_code, error_message,
                 _json(result) if result is not None else row["result_json"], _json(history), job_id))
            return self._job_dict(self._require(con, "jobs", job_id))

    def get_job(self, job_id: str) -> dict:
        with self.connection() as con:
            return self._job_dict(self._require(con, "jobs", job_id))

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self.connection() as con:
            return [self._job_dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY created_at DESC,id LIMIT ?", (max(1, min(int(limit), 500)),))]

    def cancel_job(self, job_id: str) -> dict:
        with self._write() as con:
            row = self._require(con, "jobs", job_id)
            if row["status"] in _TERMINAL_JOBS:
                return self._job_dict(row)
            history = json.loads(row["attempt_history_json"])
            history.append({"attempt": row["attempt"], "status": "cancelled", "finished_at": _now()})
            con.execute("UPDATE jobs SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=?,attempt_history_json=? WHERE id=?", (_now(), _json(history), job_id))
            return self._job_dict(self._require(con, "jobs", job_id))

    def retry_job(self, job_id: str) -> dict:
        with self._write() as con:
            row = self._require(con, "jobs", job_id)
            if row["status"] not in {"failed", "partial", "interrupted", "cancelled"}:
                raise StoreError("job_not_retryable", "Only failed, partial, interrupted, or cancelled jobs can be retried.", 409)
            if con.execute("SELECT 1 FROM jobs WHERE work_key=? AND status IN ('queued','running') AND id<>?", (row["work_key"], job_id)).fetchone():
                raise StoreError("work_already_running", "Another attempt is already queued or running.", 409)
            con.execute("""UPDATE jobs SET status='queued',lease_token=NULL,lease_until=NULL,heartbeat_at=NULL,
                progress_current=0,progress_total=0,result_json=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?""", (_now(), job_id))
            return self._job_dict(self._require(con, "jobs", job_id))

    def recover_jobs(self) -> int:
        with self._write() as con:
            expired = con.execute("SELECT * FROM jobs WHERE status='running' AND (lease_until IS NULL OR lease_until<=?)", (_now(),)).fetchall()
            for row in expired:
                history = json.loads(row["attempt_history_json"])
                history.append({"attempt": row["attempt"], "status": "interrupted", "finished_at": _now(), "error_code": "lease_expired"})
                con.execute("""UPDATE jobs SET status='interrupted',lease_token=NULL,lease_until=NULL,error_code='lease_expired',
                    error_message='The worker stopped before completing its lease.',attempt_history_json=?,updated_at=? WHERE id=?""", (_json(history), _now(), row["id"]))
            return len(expired)

    def backup(self, destination: Path | str) -> dict:
        target = Path(destination).expanduser().resolve()
        if target == self.db_path:
            raise StoreError("invalid_backup_path", "The backup must be a different file.")
        if target.exists():
            raise StoreError("backup_exists", "The backup destination already exists.", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            with sqlite3.connect(target) as dest:
                source.backup(dest)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StoreError("backup_invalid", "SQLite reported an invalid backup.", 500)
                violations = dest.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise StoreError("backup_invalid", "The backup contains invalid foreign keys.", 500)
        return {"path": str(target), "schema_version": self.SCHEMA_VERSION,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "includes_pdf_files": False}

    update_location_status = set_location_status
