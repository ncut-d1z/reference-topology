"""Complete local backups and validated, non-overwriting offline restoration."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4
import zipfile

from .storage import Store, StoreError


FORMAT_VERSION = 1
DATABASE_NAME = "reference_topology.sqlite3"
MAX_FILES = 20_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_CHUNK = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 1024 or "\\" in name or "\x00" in name:
        raise StoreError("invalid_backup", "The archive contains an invalid member path.")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts):
        raise StoreError("invalid_backup", "Archive paths must be normalized relative paths.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
    for part in path.parts:
        if ":" in part or part.endswith((".", " ")) or part.split(".", 1)[0].upper() in reserved:
            raise StoreError("invalid_backup", "An archive path is not portable between supported systems.")
    if name not in {"manifest.json", DATABASE_NAME} and path.parts[0] not in {"managed", "artifacts"}:
        raise StoreError("invalid_backup", "The archive contains a file outside the backup format.")
    return name


def _local_uri(uri: str, root: Path) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
        raise StoreError("backup_path_not_allowed", "A referenced artifact does not have a local file URI.", 409)
    try:
        path = Path(url2pathname(parsed.path)).resolve(strict=True)
        path.relative_to(root.resolve(strict=True))
    except (ValueError, OSError, RuntimeError) as error:
        raise StoreError("backup_path_not_allowed", "A referenced managed file or artifact is missing or outside its data directory.", 409) from error
    if not path.is_file():
        raise StoreError("backup_path_not_allowed", "A referenced backup member is not a regular file.", 409)
    return path


def _write_file(archive: zipfile.ZipFile, source: Path, name: str, *, root: Path,
                expected_hash: str | None = None, expected_size: int | None = None) -> dict:
    _safe_member(name)
    real = source.resolve(strict=True)
    try:
        real.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise StoreError("backup_path_not_allowed", "A backup member escaped its allowed directory.", 409) from error
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(os.open(real, flags), "rb") as input_file:
        before = os.fstat(input_file.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_MEMBER_BYTES:
            raise StoreError("backup_limit_exceeded", "A backup member is not a regular file or exceeds 512 MiB.", 413)
        with archive.open(name, "w", force_zip64=True) as output:
            while block := input_file.read(_CHUNK):
                size += len(block)
                if size > MAX_MEMBER_BYTES:
                    raise StoreError("backup_limit_exceeded", "A backup member exceeds 512 MiB.", 413)
                digest.update(block)
                output.write(block)
        after = os.fstat(input_file.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise StoreError("backup_content_changed", "A file changed while its backup was being created.", 409)
    checksum = digest.hexdigest()
    if (expected_hash and checksum != expected_hash) or (expected_size is not None and size != expected_size):
        raise StoreError("backup_content_changed", "A registered file no longer matches its recorded content.", 409)
    return {"path": name, "sha256": checksum, "size": size}


def create_backup(settings, store: Store) -> dict:
    """Snapshot SQLite first, then copy only files referenced by that snapshot.

    External linked PDFs are deliberately not copied. Their URI provenance is
    retained in the database and their count is stated in the manifest.
    """
    data_dir = Path(settings.data_dir).resolve(strict=True)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_id = uuid4().hex
    final_path = backup_dir / f"{backup_id}.zip"
    temporary_zip = backup_dir / f".{backup_id}.tmp"
    staging = Path(tempfile.mkdtemp(prefix="backup-", dir=data_dir / "tmp"))
    try:
        snapshot = staging / DATABASE_NAME
        store.backup(snapshot)
        con = sqlite3.connect(snapshot)
        con.row_factory = sqlite3.Row
        try:
            locations = con.execute("""SELECT l.*,a.sha256,a.byte_size FROM file_locations l
                JOIN pdf_assets a ON a.id=l.asset_id WHERE l.location_kind='managed' AND l.is_current=1
                AND l.host_id=? ORDER BY l.id""", (settings.host_id,)).fetchall()
            runs = con.execute("SELECT id,output_uri,output_sha256 FROM extraction_runs WHERE output_uri IS NOT NULL ORDER BY id").fetchall()
            linked_count = con.execute("SELECT COUNT(*) FROM file_locations WHERE location_kind='linked' AND is_current=1").fetchone()[0]
        finally:
            con.close()
        manifest = {"format": "reference-topology-backup", "format_version": FORMAT_VERSION,
                    "schema_version": Store.SCHEMA_VERSION, "backup_id": backup_id, "created_at": _now(),
                    "source": {"host_id": settings.host_id, "allowed_roots": [str(p) for p in settings.allowed_roots]},
                    "configuration": {"engine": settings.engine, "grobid_url": settings.grobid_url},
                    "database": DATABASE_NAME, "files": [], "managed_locations": [], "artifacts": [],
                    "linked_files": {"included": False, "location_count": linked_count,
                                     "note": "Original linked PDFs remain outside the archive; rebind their locations after restoration."}}
        seen: dict[str, dict] = {}
        total = 0
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            db_entry = _write_file(archive, snapshot, DATABASE_NAME, root=staging)
            db_entry["kind"] = "database"
            manifest["files"].append(db_entry)
            total += db_entry["size"]
            for location in locations:
                path = _local_uri(location["canonical_uri"], data_dir / "managed")
                name = path.relative_to(data_dir).as_posix()
                if name not in seen:
                    entry = _write_file(archive, path, name, root=data_dir / "managed",
                                        expected_hash=location["sha256"], expected_size=location["byte_size"])
                    entry["kind"] = "managed"
                    seen[name] = entry
                    manifest["files"].append(entry)
                    total += entry["size"]
                elif seen[name]["sha256"] != location["sha256"]:
                    raise StoreError("backup_content_changed", "Multiple content identities claim the same managed file.", 409)
                manifest["managed_locations"].append({"location_id": location["id"], "asset_id": location["asset_id"], "path": name})
            for run in runs:
                path = _local_uri(run["output_uri"], data_dir / "artifacts")
                name = path.relative_to(data_dir).as_posix()
                if name not in seen:
                    entry = _write_file(archive, path, name, root=data_dir / "artifacts", expected_hash=run["output_sha256"])
                    entry["kind"] = "artifact"
                    seen[name] = entry
                    manifest["files"].append(entry)
                    total += entry["size"]
                manifest["artifacts"].append({"run_id": run["id"], "path": name})
            if total > MAX_TOTAL_BYTES or len(manifest["files"]) + 1 > MAX_FILES:
                raise StoreError("backup_limit_exceeded", "Backup exceeds the 2 GiB or 20,000-file limit.", 413)
            manifest_bytes = _dump(manifest).encode("utf-8")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise StoreError("backup_limit_exceeded", "The backup manifest exceeds 4 MiB.", 413)
            archive.writestr("manifest.json", manifest_bytes)
        os.replace(temporary_zip, final_path)
        return {"backup_id": backup_id, "path": str(final_path), "archive_path": str(final_path),
                "archive_name": final_path.name, "size": final_path.stat().st_size, "manifest": manifest}
    except (OSError, zipfile.BadZipFile) as error:
        raise StoreError("backup_failed", f"Could not create a complete backup: {error}", 409) from error
    finally:
        temporary_zip.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def _schema(con: sqlite3.Connection) -> dict:
    return {row[0]: (row[1], row[2], row[3]) for row in con.execute(
        "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")}


def _check_database(database: Path, expected_schema: dict) -> sqlite3.Connection:
    con = sqlite3.connect(database, isolation_level=None)
    con.row_factory = sqlite3.Row
    deadline = time.monotonic() + 15
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        if _schema(con) != expected_schema:
            raise StoreError("invalid_backup_schema", "The database schema differs from the supported application schema.")
        if con.execute("PRAGMA user_version").fetchone()[0] != Store.SCHEMA_VERSION:
            raise StoreError("invalid_backup_schema", "The database schema version is not supported.")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchone():
            raise StoreError("invalid_backup_database", "The database failed integrity or foreign-key verification.")
        con.execute("PRAGMA foreign_keys=ON")
        return con
    except Exception:
        con.close()
        raise


def restore_backup(archive: Path, destination: Path) -> dict:
    """Validate all members, restore into a sibling staging directory, then rename.

    The restored installation gets a new host identity and no active external
    roots. Managed PDFs are available immediately; linked PDFs need rebinding.
    """
    archive = Path(archive).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise StoreError("restore_destination_not_empty", "Restore requires a nonexistent or empty directory.", 409)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    if archive.stat().st_size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + MAX_FILES * 2048:
        raise StoreError("backup_limit_exceeded", "The archive exceeds the restore size limit.", 413)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-restore-", dir=destination.parent))
    expected_dir = Path(tempfile.mkdtemp(prefix="reference-schema-", dir=destination.parent))
    new_host = str(uuid4())
    try:
        expected = Store(expected_dir / DATABASE_NAME)
        with expected.connection() as reference_con:
            expected_schema = _schema(reference_con)
        with zipfile.ZipFile(archive, "r") as zipped:
            infos = zipped.infolist()
            if len(infos) > MAX_FILES:
                raise StoreError("backup_limit_exceeded", "The archive contains too many files.", 413)
            names = set()
            folded = set()
            for info in infos:
                name = _safe_member(info.filename)
                if name in names or name.casefold() in folded:
                    raise StoreError("invalid_backup", "The archive contains duplicate or case-colliding paths.")
                names.add(name)
                folded.add(name.casefold())
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}) or info.flag_bits & 1:
                    raise StoreError("invalid_backup", "Directories, symlinks, special files, and encrypted members are not accepted.")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise StoreError("backup_limit_exceeded", "An archive member exceeds 512 MiB.", 413)
            if "manifest.json" not in names:
                raise StoreError("invalid_backup", "The archive has no manifest.")
            if zipped.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise StoreError("backup_limit_exceeded", "The manifest exceeds 4 MiB.", 413)
            manifest = json.loads(zipped.read("manifest.json"))
            if not isinstance(manifest, dict):
                raise StoreError("invalid_backup", "The manifest must be a JSON object.")
            if (manifest.get("format") != "reference-topology-backup" or manifest.get("format_version") != FORMAT_VERSION
                    or manifest.get("schema_version") != Store.SCHEMA_VERSION or manifest.get("database") != DATABASE_NAME):
                raise StoreError("invalid_backup", "Unsupported backup format or schema version.")
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) > MAX_FILES - 1:
                raise StoreError("invalid_backup", "The manifest file list is invalid.")
            by_path = {}
            total = 0
            for record in records:
                if not isinstance(record, dict):
                    raise StoreError("invalid_backup", "The manifest contains a malformed file record.")
                name = _safe_member(record.get("path", ""))
                size = record.get("size")
                checksum = record.get("sha256", "")
                if name == "manifest.json" or name in by_path or not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_MEMBER_BYTES or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise StoreError("invalid_backup", "The manifest contains an invalid file record.")
                by_path[name] = record
                total += size
            if total > MAX_TOTAL_BYTES:
                raise StoreError("backup_limit_exceeded", "The expanded backup exceeds 2 GiB.", 413)
            if set(by_path) | {"manifest.json"} != names or DATABASE_NAME not in by_path:
                raise StoreError("invalid_backup", "Archive members and manifest records do not match.")
            for name, record in by_path.items():
                info = zipped.getinfo(name)
                if info.file_size != record["size"]:
                    raise StoreError("invalid_backup", "Archive and manifest sizes disagree.")
                target = stage.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with zipped.open(info) as src, target.open("xb") as dst:
                    while chunk := src.read(_CHUNK):
                        size += len(chunk)
                        if size > record["size"] or size > MAX_MEMBER_BYTES:
                            raise StoreError("backup_limit_exceeded", "A member expands beyond its declared size.", 413)
                        digest.update(chunk)
                        dst.write(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise StoreError("backup_hash_mismatch", "A backup member failed its SHA-256 verification.")
        database = stage / DATABASE_NAME
        con = _check_database(database, expected_schema)
        try:
            # Verify migration checksums as well as structural schema equality.
            expected_rows = []
            with expected.connection() as reference_con:
                expected_rows = [tuple(r) for r in reference_con.execute("SELECT version,name,checksum FROM schema_migrations ORDER BY version")]
            if [tuple(r) for r in con.execute("SELECT version,name,checksum FROM schema_migrations ORDER BY version")] != expected_rows:
                raise StoreError("invalid_backup_schema", "Migration checksums do not match the supported schema.")
            locations = manifest.get("managed_locations", [])
            artifacts = manifest.get("artifacts", [])
            if not isinstance(locations, list) or not isinstance(artifacts, list):
                raise StoreError("invalid_backup", "The manifest location mappings are invalid.")
            source = manifest.get("source")
            if not isinstance(source, dict) or not isinstance(source.get("host_id"), str) or not source["host_id"]:
                raise StoreError("invalid_backup", "The backup has no valid source host identity.")
            expected_locations = {row[0] for row in con.execute("SELECT id FROM file_locations WHERE location_kind='managed' AND is_current=1 AND host_id=?", (source["host_id"],))}
            used_locations, used_runs, used_files = set(), set(), {DATABASE_NAME}
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE file_locations SET status='missing' WHERE location_kind='linked'")
            con.execute("UPDATE file_locations SET status='missing' WHERE location_kind='managed'")
            for mapping in locations:
                if not isinstance(mapping, dict):
                    raise StoreError("invalid_backup", "The managed-file map is malformed.")
                name = _safe_member(mapping.get("path", ""))
                location_id = mapping.get("location_id")
                record = by_path.get(name)
                row = con.execute("""SELECT l.*,a.sha256,a.byte_size FROM file_locations l JOIN pdf_assets a ON a.id=l.asset_id WHERE l.id=?""", (location_id,)).fetchone()
                if (location_id in used_locations or not row or row["location_kind"] != "managed" or not row["is_current"]
                        or row["asset_id"] != mapping.get("asset_id") or not record or not name.startswith("managed/")
                        or record["sha256"] != row["sha256"] or record["size"] != row["byte_size"]):
                    raise StoreError("invalid_backup", "A managed-file mapping does not match the database content identity.")
                used_locations.add(location_id)
                used_files.add(name)
                con.execute("""UPDATE file_locations SET host_id=?,canonical_uri=?,root_key='managed',
                    status='available',last_checked_at=? WHERE id=?""", (new_host, (destination / name).as_uri(), _now(), location_id))
            expected_runs = {row[0] for row in con.execute("SELECT id FROM extraction_runs WHERE output_uri IS NOT NULL")}
            # Relocation changes a locator, not extraction evidence. Disable only
            # the known, schema-verified trigger inside this offline transaction
            # and restore its exact definition before committing.
            trigger_sql = expected_schema["run_input_immutable"][2]
            con.execute("DROP TRIGGER run_input_immutable")
            for mapping in artifacts:
                if not isinstance(mapping, dict):
                    raise StoreError("invalid_backup", "The artifact map is malformed.")
                name = _safe_member(mapping.get("path", ""))
                run_id = mapping.get("run_id")
                row = con.execute("SELECT output_sha256 FROM extraction_runs WHERE id=?", (run_id,)).fetchone()
                record = by_path.get(name)
                if (run_id in used_runs or run_id not in expected_runs or row is None or not record or not name.startswith("artifacts/")
                        or (row["output_sha256"] and row["output_sha256"] != record["sha256"])):
                    raise StoreError("invalid_backup", "An artifact mapping does not match its extraction evidence.")
                used_runs.add(run_id)
                used_files.add(name)
                con.execute("UPDATE extraction_runs SET output_uri=? WHERE id=?", ((destination / name).as_uri(), run_id))
            con.execute(trigger_sql)
            if used_locations != expected_locations or used_runs != expected_runs or used_files != set(by_path):
                raise StoreError("invalid_backup", "Referenced artifacts are missing or unreferenced files were added to the archive.")
            for job in con.execute("SELECT id,attempt,attempt_history_json FROM jobs WHERE status IN ('queued','running')").fetchall():
                history = json.loads(job["attempt_history_json"])
                history.append({"attempt": job["attempt"], "status": "interrupted", "finished_at": _now(), "error_code": "restored_backup"})
                con.execute("""UPDATE jobs SET status='interrupted',lease_token=NULL,lease_until=NULL,
                    error_code='restored_backup',error_message='Restored tasks require an explicit retry.',
                    attempt_history_json=?,updated_at=? WHERE id=?""", (json.dumps(history), _now(), job["id"]))
            if con.execute("PRAGMA foreign_key_check").fetchone():
                raise StoreError("invalid_backup_database", "Restored file mappings violate database relationships.")
            con.commit()
            if _schema(con) != expected_schema or con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError("invalid_backup_database", "The restored database failed final verification.")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()
        settings = {"host_id": new_host, "allowed_roots": [], "engine": "python-rules", "grobid_url": "http://127.0.0.1:8070"}
        (stage / "settings.json").write_text(_dump(settings), encoding="utf-8")
        (stage / "restore-info.json").write_text(_dump({"backup_id": manifest.get("backup_id"), "restored_at": _now(),
            "source": manifest.get("source"), "original_configuration": manifest.get("configuration"),
            "linked_files_require_rebinding": True}), encoding="utf-8")
        for folder in ("managed", "artifacts", "tmp", "backups"):
            (stage / folder).mkdir(exist_ok=True)
        # rmdir refuses nonempty directories even if another process changed the
        # destination since the initial check. Never merge restored files in place.
        if destination.exists():
            if destination.is_symlink():
                raise StoreError("restore_destination_not_empty", "The restore destination changed.", 409)
            destination.rmdir()
        os.replace(stage, destination)
        return {"backup_id": manifest.get("backup_id"), "destination": str(destination), "host_id": new_host,
                "restored_files": len(by_path), "managed_locations": len(used_locations), "artifacts": len(used_runs),
                "linked_files_require_rebinding": True, "schema_version": Store.SCHEMA_VERSION}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError, zipfile.BadZipFile) as error:
        if isinstance(error, StoreError):
            raise
        raise StoreError("invalid_backup", f"Backup restoration failed validation: {error}") from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(expected_dir, ignore_errors=True)
