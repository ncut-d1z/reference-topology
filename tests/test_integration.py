"""Cross-module regressions for the actual background worker and recovery UI."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from reference_topology.app import create_app
from reference_topology.config import Settings


def _pdf(title="Background worker integration", doi="10.9998/integration"):
    output = io.BytesIO()
    doc = canvas.Canvas(output)
    doc.setTitle(title)
    doc.setAuthor("Alice Example")
    lines = [title, "Alice Example", f"DOI: {doi}", "Abstract",
             "This generated document exercises the persistent parse queue.",
             "References", '[1] B. Example. "Cited integration work". Journal of Testing, 2020.',
             "doi:10.9998/cited"]
    for index, line in enumerate(lines):
        doc.drawString(45, 790 - index * 24, line)
    doc.showPage()
    doc.save()
    return output.getvalue()


def _connect(client):
    result = client.get("/api/session")
    assert result.status_code == 200
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]


def _register(client, settings, name):
    response = client.post("/api/files/register", json={"root_id": settings.roots()[0]["id"], "paths": [name]})
    assert response.status_code == 202, response.text
    assert response.json()["errors"] == [], response.text
    return response.json()["items"][0]


def _wait(client, job_id, timeout=8):
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.05)
    raise AssertionError(f"Background job failed to finish: {result}")


def _settings(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    return Settings(data_dir=tmp_path / "data", allowed_roots=[root]), root


def _cli(*arguments):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run([sys.executable, "-m", "reference_topology", *map(str, arguments)],
                             env=env, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_background_worker_http_backup_and_cli_restore_are_connected(tmp_path):
    settings, root = _settings(tmp_path)
    app = create_app(settings, run_worker=True)
    data = _pdf()
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        _connect(client)
        response = client.post("/api/files/upload", files=[("files", ("sample.pdf", data, "application/pdf"))])
        assert response.status_code == 202 and not response.json()["errors"], response.text
        item = response.json()["items"][0]
        job = _wait(client, item["job_id"])
        assert job["status"] == "succeeded", job
        assert client.get(f"/api/papers/{item['paper_id']}").json()["doi"] == "10.9998/integration"
        refs = client.get(f"/api/papers/{item['paper_id']}/references").json()["items"]
        assert len(refs) == 1
        response = client.post("/api/backups")
        assert response.status_code == 200, response.text
        backup = response.json()
        download = client.get(backup["download_url"])
        assert download.status_code == 200
        archive = tmp_path / "downloaded.zip"
        archive.write_bytes(download.content)
        with zipfile.ZipFile(archive) as zipped:
            manifest = json.loads(zipped.read("manifest.json"))
            assert manifest["managed_locations"]
            assert manifest["artifacts"]
    assert not app.state.runner._thread.is_alive()
    destination = tmp_path / "restored"
    restored = _cli("--restore", archive, "--data-dir", destination)
    assert restored["host_id"] != settings.host_id
    restored_settings = Settings.load(data_dir=destination)
    restored_app = create_app(restored_settings, run_worker=False)
    with TestClient(restored_app, base_url="http://127.0.0.1:8765") as client:
        _connect(client)
        assert client.get(f"/api/files/{item['file_id']}/content").content == data
        assert len(client.get(f"/api/papers/{item['paper_id']}/references").json()["items"]) == 1
    cli_backup = _cli("--backup", "--data-dir", destination)
    assert Path(cli_backup["path"]).is_file()


def test_changed_file_fails_then_rebind_and_retry_complete_in_background(tmp_path):
    settings, root = _settings(tmp_path)
    data = _pdf()
    current = root / "article.pdf"
    current.write_bytes(data)
    original_copy = root / "original-copy.pdf"
    original_copy.write_bytes(data)
    app = create_app(settings, run_worker=True)
    # Queue before lifespan startup, then replace the registered bytes.
    item = app.state.files.register(settings.roots()[0]["id"], current.name)
    job = app.state.runner.enqueue(item["file_id"], item["paper_id"])
    current.write_bytes(_pdf("Different content", "10.9998/replacement"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        _connect(client)
        failed = _wait(client, job["id"])
        assert failed["status"] == "failed" and failed["error_code"] == "content_changed", failed
        assert client.get(f"/api/papers/{item['paper_id']}/references").json()["items"] == []
        response = client.post(f"/api/files/{item['file_id']}/rebind", json={
            "root_id": settings.roots()[0]["id"], "path": original_copy.name})
        assert response.status_code == 200, response.text
        assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 202
        finished = _wait(client, job["id"])
        assert finished["status"] == "succeeded" and finished["attempt"] == 2, finished
        assert client.get(f"/api/files/{item['file_id']}/content").content == data
        assert client.get(f"/api/papers/{item['paper_id']}").json()["doi"] == "10.9998/integration"


def test_missing_saved_root_does_not_block_startup_and_rebinding(tmp_path):
    settings, root = _settings(tmp_path)
    data = _pdf()
    (root / "article.pdf").write_bytes(data)
    app = create_app(settings, run_worker=False)
    item = app.state.files.register(settings.roots()[0]["id"], "article.pdf")
    moved_root = tmp_path / "moved-papers"
    root.rename(moved_root)
    # A disconnected drive or renamed root must not prevent the recovery UI.
    loaded = Settings.load(data_dir=settings.data_dir)
    app2 = create_app(loaded, run_worker=False)
    with TestClient(app2, base_url="http://127.0.0.1:8765") as client:
        _connect(client)
        assert client.get(f"/api/files/{item['file_id']}/content").status_code == 404
        response = client.put("/api/settings", json={"allowed_roots": [str(moved_root)]})
        assert response.status_code == 200, response.text
        response = client.post(f"/api/files/{item['file_id']}/rebind", json={
            "root_id": loaded.roots()[0]["id"], "path": "article.pdf"})
        assert response.status_code == 200, response.text
        assert client.get(f"/api/files/{item['file_id']}/content").content == data


def test_worker_restarts_with_a_second_lifespan(tmp_path):
    settings, root = _settings(tmp_path)
    (root / "first.pdf").write_bytes(_pdf())
    (root / "second.pdf").write_bytes(_pdf("Second article", "10.9998/second"))
    app = create_app(settings, run_worker=True)
    for filename in ("first.pdf", "second.pdf"):
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            _connect(client)
            item = _register(client, settings, filename)
            assert _wait(client, item["job_id"], timeout=3)["status"] == "succeeded"


def test_completed_publication_cannot_be_reported_as_cancelled(tmp_path, monkeypatch):
    settings, root = _settings(tmp_path)
    (root / "article.pdf").write_bytes(_pdf())
    app = create_app(settings, run_worker=True)
    published, release = threading.Event(), threading.Event()
    publish_parse = app.state.store.publish_parse

    def pause_after_publication(*args, **kwargs):
        result = publish_parse(*args, **kwargs)
        published.set()
        if not release.wait(timeout=5):
            raise AssertionError("Test did not release the completed publication")
        return result

    monkeypatch.setattr(app.state.store, "publish_parse", pause_after_publication)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        _connect(client)
        item = _register(client, settings, "article.pdf")
        try:
            assert published.wait(timeout=5)
            response = client.post(f"/api/jobs/{item['job_id']}/cancel")
            assert response.status_code == 200, response.text
            # The snapshot is already committed. Cancellation may either have
            # won before commit (no snapshot) or see a completed terminal job.
            refs = client.get(f"/api/papers/{item['paper_id']}/references").json()["items"]
            assert not refs or response.json()["status"] in {"succeeded", "partial"}, response.text
        finally:
            release.set()
