"""FastAPI entry point for the local workbench."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
from typing import Any
import uuid

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Settings
from .files import FileError, FileService
from .jobs import JobRunner
from .security import LocalAccessMiddleware
from .storage import Store, StoreError


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Registration(Payload):
    root_id: str
    paths: list[str] = Field(min_length=1, max_length=100)
    request_key: str | None = Field(default=None, max_length=200)


class Rebinding(Payload):
    root_id: str
    path: str


class ParserRequest(Payload):
    engine: str | None = None
    force: bool = True


class Preferences(Payload):
    allowed_roots: list[str] | None = Field(default=None, max_length=50)
    engine: str | None = None


class PaperEdit(Payload):
    fields: dict[str, Any]
    expected_version: int | None = None


class Decision(Payload):
    action: str
    target_paper_id: str | None = None
    metadata: dict[str, Any] | None = None
    expected_assertion_id: str | None = None
    reason: str = Field(default="用户核对参考文献", max_length=2000)


class ReferenceRevision(Payload):
    base_run_id: str
    references: list[dict] = Field(max_length=5000)


class Merge(Payload):
    from_paper_id: str
    to_paper_id: str
    reason: str = Field(default="用户确认重复文献", max_length=2000)


class PDFSnapshotResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    settings = settings or Settings.load()
    settings.save()
    store = Store(settings.data_dir / "reference_topology.sqlite3")
    files = FileService(settings, store)
    runner = JobRunner(settings, store, files)
    session_id, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if run_worker:
            runner.start()
        yield
        if run_worker:
            runner.stop()

    app = FastAPI(title="Reference Topology", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.files, app.state.runner, app.state.settings = store, files, runner, settings
    app.add_middleware(LocalAccessMiddleware, settings=settings, session_id=session_id, csrf_token=csrf_token)

    @app.exception_handler(StoreError)
    @app.exception_handler(FileError)
    async def domain_error(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status_code)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"error": {"code": "invalid_input", "message": str(error)}}, status_code=400)

    @app.exception_handler(OSError)
    async def io_error(request, error):
        return JSONResponse({"error": {"code": "file_unavailable", "message": "无法访问所选文件或目录，请检查位置和权限"}}, status_code=400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def constraint_error(request, error):
        return JSONResponse({"error": {"code": "data_conflict", "message": "数据与现有记录冲突，请刷新并复核"}}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"error": {"code": "invalid_request", "message": "请求字段或取值不符合要求",
                            "fields": [".".join(str(v) for v in e["loc"]) for e in error.errors()]}}, status_code=422)

    static = Path(__file__).parent / "static"
    # HTML and JavaScript must be deployed as a matched pair. A new HTML page
    # paired with a cached, older app.js leaves newly added controls inert.
    # Content-derived URLs avoid relying on users to clear their browser cache.
    js_revision = hashlib.sha256((static / "app.js").read_bytes()).hexdigest()[:16]
    css_revision = hashlib.sha256((static / "styles.css").read_bytes()).hexdigest()[:16]
    homepage = (static / "index.html").read_text(encoding="utf-8")
    homepage = homepage.replace('src="/static/app.js"', f'src="/static/app.js?v={js_revision}"')
    homepage = homepage.replace('href="/static/styles.css"', f'href="/static/styles.css?v={css_revision}"')

    @app.get("/")
    def home():
        return HTMLResponse(homepage)  # LocalAccessMiddleware already sets no-store.

    @app.get("/api/session")
    def session():
        response = JSONResponse({"csrf_token": csrf_token, "version": __version__})
        response.set_cookie("rt_session", session_id, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "schema_version": store.SCHEMA_VERSION,
                "local_parser": True, "network_enabled": False}

    @app.get("/api/settings")
    def preferences():
        return settings.public()

    @app.put("/api/settings")
    def update_preferences(body: Preferences):
        settings.update(roots=body.allowed_roots, engine=body.engine)
        return settings.public()

    @app.get("/api/roots")
    def roots():
        return {"items": settings.roots()}

    @app.get("/api/files/browse")
    def browse(root_id: str, path: str = "", offset: int = Query(default=0, ge=0)):
        return files.browse(root_id, path, offset)

    @app.post("/api/files/register", status_code=202)
    def register(body: Registration):
        items, errors = [], []
        paths = list(dict.fromkeys(body.paths))
        for index, relative in enumerate(paths):
            try:
                item = files.register(body.root_id, relative)
                # The content hash identifies a PDF asset; duplicate paths may
                # still be registered as alternate locations, without new jobs.
                request_key = f"{body.request_key}:{index}" if body.request_key else None
                job = runner.enqueue(item["file_id"], item["paper_id"], request_key=request_key) if item["created"] else None
                items.append({**item, "duplicate": not item["created"],
                              "duplicate_reason": "identical_content" if not item["created"] else None,
                              "job_id": job["id"] if job else None,
                              "path": relative, "name": Path(relative).name})
            except (StoreError, FileError, OSError) as error:
                errors.append({"path": relative, "name": Path(relative).name,
                               "code": getattr(error, "code", "file_unavailable"), "message": str(error)})
        # Report even repeated path entries that were skipped before hashing.
        by_path = {item["path"]: item for item in items}
        skipped, seen = [], set()
        for relative in body.paths:
            if relative in seen:
                previous = by_path.get(relative)
                skipped.append({
                    "path": relative, "name": Path(relative).name,
                    "duplicate_reason": "repeated_path",
                    "duplicate_of": ({
                        "file_id": previous["file_id"], "paper_id": previous["paper_id"],
                        "file_name": previous["name"], "file_uri": None,
                        "title": previous.get("duplicate_of", {}).get("title") if previous.get("duplicate_of") else None,
                        "year": previous.get("duplicate_of", {}).get("year") if previous.get("duplicate_of") else None,
                        "doi": previous.get("duplicate_of", {}).get("doi") if previous.get("duplicate_of") else None,
                    } if previous else None),
                })
            seen.add(relative)
        return {"items": items, "errors": errors, "skipped": skipped,
                "summary": {"added": sum(bool(item["created"]) for item in items),
                            "duplicates": sum(bool(item["duplicate"]) for item in items) + len(skipped),
                            "failed": len(errors)}}

    @app.post("/api/files/upload", status_code=202)
    async def upload(files_input: list[UploadFile] = File(alias="files")):
        if len(files_input) > 20:
            raise FileError("too_many_files", "每次最多上传 20 个 PDF")
        items, errors = [], []
        limit = settings.max_pdf_size_mb * 1024 * 1024
        for uploaded in files_input:
            temporary = None
            try:
                if not (uploaded.filename or "").lower().endswith(".pdf"):
                    raise FileError("invalid_pdf", "请选择 PDF 文件")
                with tempfile.NamedTemporaryFile(dir=settings.data_dir / "tmp", suffix=".pdf", delete=False) as stream:
                    temporary = Path(stream.name)
                    count = 0
                    while chunk := await uploaded.read(1024 * 1024):
                        count += len(chunk)
                        if count > limit:
                            raise FileError("file_too_large", "PDF 超过配置大小上限", 413)
                        stream.write(chunk)
                item = await run_in_threadpool(files.register_upload, temporary, uploaded.filename)
                job = await run_in_threadpool(runner.enqueue, item["file_id"], item["paper_id"]) if item["created"] else None
                items.append({**item, "duplicate": not item["created"],
                              "duplicate_reason": "identical_content" if not item["created"] else None,
                              "job_id": job["id"] if job else None, "name": uploaded.filename})
            except (StoreError, FileError, OSError) as error:
                errors.append({"name": uploaded.filename, "code": getattr(error, "code", "file_unavailable"), "message": str(error)})
            finally:
                await uploaded.close()
                if temporary:
                    temporary.unlink(missing_ok=True)
        return {"items": items, "errors": errors,
                "summary": {"added": sum(bool(item["created"]) for item in items),
                            "duplicates": sum(bool(item["duplicate"]) for item in items),
                            "failed": len(errors)}}

    @app.api_route("/api/files/{file_id}/content", methods=["GET", "HEAD"])
    def content(file_id: str):
        snapshot, location = files.snapshot(file_id)
        asset = store.get_asset(file_id)
        return PDFSnapshotResponse(snapshot, media_type="application/pdf", filename=location["display_name"],
                content_disposition_type="inline", headers={"ETag": f'"{asset["sha256"]}"'})

    @app.post("/api/files/{file_id}/rebind")
    def rebind(file_id: str, body: Rebinding):
        return files.rebind(file_id, body.root_id, body.path)

    @app.post("/api/files/{file_id}/parse", status_code=202)
    def parse(file_id: str, body: ParserRequest):
        asset = store.get_asset(file_id)
        with store.connection() as con:
            row = con.execute("SELECT paper_id FROM paper_files WHERE asset_id=? ORDER BY is_preferred DESC LIMIT 1", (asset["id"],)).fetchone()
        if not row:
            raise StoreError("paper_missing", "PDF 尚未关联文献", 409)
        job = runner.enqueue(file_id, row[0], engine=body.engine, force=body.force)
        return {**job, "job_id": job["id"]}

    @app.get("/api/jobs")
    def jobs():
        return {"items": store.list_jobs(limit=100)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return store.get_job(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return store.cancel_job(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        return store.retry_job(job_id)

    @app.get("/api/papers")
    def papers(q: str = "", limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.list_papers(q, limit, offset)

    @app.get("/api/papers/{paper_id}")
    def paper(paper_id: str):
        return store.paper_detail(paper_id)

    @app.patch("/api/papers/{paper_id}")
    def edit(paper_id: str, body: PaperEdit):
        result = store.edit_paper(paper_id, body.fields, body.expected_version)
        store.match_pending()
        return result

    @app.get("/api/papers/{paper_id}/references")
    def references(paper_id: str):
        return {"items": store.list_references(paper_id)}

    @app.get("/api/papers/{paper_id}/citations")
    def citations(paper_id: str):
        return {"items": store.list_citations(paper_id), "scope": "local-known"}

    @app.post("/api/references/{entry_id}/decisions")
    def decide(entry_id: str, body: Decision):
        return store.decide_reference(entry_id, body.action, body.target_paper_id, body.metadata,
                                     expected_assertion_id=body.expected_assertion_id, reason=body.reason)

    @app.post("/api/papers/{paper_id}/reference-revisions")
    def revise(paper_id: str, body: ReferenceRevision):
        from .parsing import parse_reference_text
        references = [{**item, "parsed": item.get("parsed") or parse_reference_text(item.get("raw_text", ""))}
                      for item in body.references]
        return store.revise_references(paper_id, body.base_run_id, references)

    @app.post("/api/paper-merges")
    def merge(body: Merge):
        return store.merge_papers(body.from_paper_id, body.to_paper_id, body.reason)

    @app.post("/api/paper-merges/{merge_id}/revert")
    def revert(merge_id: str):
        return store.revert_merge(merge_id)

    @app.get("/api/graph")
    def graph(center: str | None = None, direction: str = "both", depth: int = Query(default=1, ge=0),
              limit: int = Query(default=300, ge=1), include_pending: bool = False):
        if direction not in {"both", "in", "out"}:
            raise ValueError("direction 必须为 both、in 或 out")
        if depth > settings.maximum_depth:
            raise ValueError("超出图的深度上限")
        return store.graph(center or None, direction, depth, min(limit, settings.maximum_nodes),
                           settings.maximum_edges, include_pending)

    @app.get("/api/graph/edges")
    def edge(source: str, target: str):
        return {"items": store.edge_evidence(source, target)}

    def all_papers():
        result, offset = [], 0
        while True:
            page = store.list_papers(limit=1000, offset=offset)
            result.extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                return result

    @app.get("/api/exports/{format}")
    def export(format: str):
        if format == "graph-json":
            # Export uses graph limits deliberately and carries truncation metadata.
            payload = store.graph(limit=settings.maximum_nodes, max_edges=settings.maximum_edges)
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            filename, mime = "reference-graph.json", "application/json"
        elif format == "csl-json":
            payload = []
            for item in all_papers():
                record = {"id": item["id"], "type": "article-journal", "title": item["title"] or item["display_title"],
                          "author": [{"literal": a["display_name"]} for a in item["authors"]]}
                for key, target in (("doi", "DOI"), ("venue", "container-title"), ("volume", "volume"), ("issue", "issue"), ("pages", "page")):
                    if item.get(key):
                        record[target] = item[key]
                if item.get("year"):
                    record["issued"] = {"date-parts": [[item["year"]]]}
                payload.append(record)
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            filename, mime = "references.csl.json", "application/json"
        elif format == "bibtex":
            def escape(value):
                return "".join({"\\": "\\textbackslash{}", "{": "\\{", "}": "\\}", "%": "\\%", "&": "\\&", "_": "\\_", "#": "\\#", "$": "\\$"}.get(c, c) for c in str(value))
            blocks = []
            for item in all_papers():
                fields = {"title": item["title"] or item["display_title"], "author": " and ".join(a["display_name"] for a in item["authors"]),
                          "year": item["year"], "doi": item.get("doi"), "journal": item.get("venue")}
                lines = [f"  {key} = {{{escape(value)}}}" for key, value in fields.items() if value]
                blocks.append("@article{ref_" + item["id"].replace("-", "") + ",\n" + ",\n".join(lines) + "\n}")
            body = "\n\n".join(blocks) + "\n"
            filename, mime = "references.bib", "application/x-bibtex"
        else:
            raise StoreError("unknown_format", "支持 csl-json、bibtex、graph-json", 404)
        return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/backups")
    def backup():
        from .maintenance import create_backup
        result = create_backup(settings, store)
        return {**result, "download_url": f"/api/backups/{result['backup_id']}/download"}

    @app.get("/api/backups/{backup_id}/download")
    def download_backup(backup_id: str):
        try:
            if uuid.UUID(backup_id).hex != backup_id:
                raise ValueError()
        except ValueError:
            raise StoreError("backup_missing", "备份不存在", 404)
        path = settings.data_dir / "backups" / f"{backup_id}.zip"
        if not path.is_file():
            raise StoreError("backup_missing", "备份不存在", 404)
        return FileResponse(path, filename=f"reference-topology-{backup_id}.zip", media_type="application/zip")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
