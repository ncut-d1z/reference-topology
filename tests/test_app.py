"""Exercise the real HTTP routes, PDF worker, file identity and browser boundaries."""
from pathlib import Path
import io

from fastapi.testclient import TestClient
import pytest
from reportlab.pdfgen import canvas

from reference_topology.app import create_app
from reference_topology.config import Settings


def pdf_bytes(title="Spherical Motion Studies", doi="10.9999/spherical.a", references=True):
    stream = io.BytesIO()
    doc = canvas.Canvas(stream)
    doc.setTitle(title)
    doc.setAuthor("Alice Example")
    lines = [title, "Alice Example", "Journal of Geometry, 2024", f"DOI: {doi}",
             "Abstract", "This paper studies reliable camera motion from observations.",
             "We evaluate the method with a reproducible experimental comparison.",
             "The main article identifier above belongs to this paper."]
    if references:
        lines += ["References", '[1] B. Example. "Robust Camera Tracking". Journal of Vision, 2020.',
                  "doi:10.9999/tracking.b"]
    for i, line in enumerate(lines):
        doc.drawString(45, 795 - 22 * i, line)
    doc.showPage()
    doc.save()
    return stream.getvalue()


@pytest.fixture
def environment(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    settings = Settings(data_dir=tmp_path / "data", allowed_roots=[papers])
    app = create_app(settings, run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["csrf_token"]
        client.headers["X-CSRF-Token"] = token
        yield app, client, papers, settings


def register(client, settings, name):
    response = client.post("/api/files/register", json={"root_id": settings.roots()[0]["id"], "paths": [name]})
    assert response.status_code == 202, response.text
    assert response.json()["errors"] == [], response.text
    return response.json()["items"][0]


def run_jobs(app):
    while job := app.state.store.claim_job(lease_seconds=30):
        app.state.runner.run(job)
        result = app.state.store.get_job(job["id"])
        assert result["status"] in {"succeeded", "partial"}, result


def test_registered_unicode_uri_ranges_and_repeat_import(environment):
    app, client, papers, settings = environment
    filename = "论文 # one.pdf"
    data = pdf_bytes()
    (papers / filename).write_bytes(data)
    first = register(client, settings, filename)
    second = register(client, settings, filename)
    assert first["file_id"] == second["file_id"]
    assert first["paper_id"] == second["paper_id"]
    asset = app.state.store.get_asset(first["file_id"])
    assert asset["locations"][0]["original_uri"] == (papers / filename).as_uri()
    url = f"/api/files/{first['file_id']}/content"
    response = client.get(url)
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-disposition"].startswith("inline")
    response = client.get(url, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206 and response.content == data[:16]
    assert client.head(url).headers["content-length"] == str(len(data))
    assert client.get(url, headers={"Range": "bytes=99999999-"}).status_code == 416
    assert list((settings.data_dir / "tmp").iterdir()) == []


def test_move_rebind_and_changed_bytes_never_open_as_old_file(environment):
    app, client, papers, settings = environment
    original = papers / "a.pdf"
    original.write_bytes(pdf_bytes())
    item = register(client, settings, original.name)
    moved = papers / "moved.pdf"
    original.rename(moved)
    url = f"/api/files/{item['file_id']}/content"
    assert client.get(url).status_code == 404
    result = client.post(f"/api/files/{item['file_id']}/rebind", json={"root_id": settings.roots()[0]["id"], "path": moved.name})
    assert result.status_code == 200, result.text
    assert client.get(url).content == moved.read_bytes()
    moved.write_bytes(pdf_bytes("Different Article", "10.9999/different"))
    assert client.get(url).status_code == 409
    assert client.get(f"/api/papers/{item['paper_id']}").status_code == 200


def test_path_traversal_and_symlink_are_rejected(environment, tmp_path):
    app, client, papers, settings = environment
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    attempts = ["../outside.pdf"]
    try:
        (papers / "escape.pdf").symlink_to(outside)
        attempts.append("escape.pdf")
    except OSError:
        pass
    for path in attempts:
        response = client.post("/api/files/register", json={"root_id": settings.roots()[0]["id"], "paths": [path]})
        assert not response.json()["items"]
        assert response.json()["errors"][0]["code"] == "path_not_allowed"
    assert app.state.store.list_papers()["total"] == 0


def test_session_csrf_origin_and_host_boundaries(environment):
    app, client, papers, settings = environment
    root = settings.roots()[0]["id"]
    with TestClient(app, base_url="http://127.0.0.1:8765") as unauthenticated:
        assert unauthenticated.get("/api/papers").status_code == 401
    token = client.headers.pop("X-CSRF-Token")
    assert client.put("/api/settings", json={"allowed_roots": []}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.put("/api/settings", json={"allowed_roots": []}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/papers", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/roots").json()["items"][0]["id"] == root


def test_upload_records_only_managed_uri(environment):
    app, client, papers, settings = environment
    data = pdf_bytes()
    response = client.post("/api/files/upload", files=[("files", ("C:\\fakepath\\article.pdf", data, "application/pdf"))])
    assert response.status_code == 202, response.text
    assert response.json()["errors"] == []
    item = response.json()["items"][0]
    asset = app.state.store.get_asset(item["file_id"])
    assert asset["locations"][0]["original_uri"] is None
    assert asset["locations"][0]["location_kind"] == "managed"
    assert client.get(f"/api/files/{item['file_id']}/content").content == data
    assert client.post("/api/files/upload", files=[("files", ("bad.pdf", b"not a PDF", "application/pdf"))]).json()["errors"][0]["code"] == "invalid_pdf"


def test_real_parse_review_graph_later_import_and_manual_override(environment):
    app, client, papers, settings = environment
    (papers / "a.pdf").write_bytes(pdf_bytes())
    imported = register(client, settings, "a.pdf")
    run_jobs(app)
    source = client.get(f"/api/papers/{imported['paper_id']}").json()
    assert source["doi"] == "10.9999/spherical.a"
    references = client.get(f"/api/papers/{source['id']}/references").json()["items"]
    assert len(references) == 1
    ref = references[0]
    assert "Robust Camera Tracking" in ref["raw_text"]
    body = {"action": "accept", "target_paper_id": ref["target"]["id"]} if ref["target"] else {
        "action": "accept", "metadata": {"title": "Robust Camera Tracking", "doi": "10.9999/tracking.b", "year": 2020}}
    decision = client.post(f"/api/references/{ref['id']}/decisions", json=body)
    assert decision.status_code == 200, decision.text
    target_id = decision.json()["target"]["id"]
    graph = client.get("/api/graph", params={"center": source["id"]}).json()
    assert any(e["source"] == source["id"] and e["target"] == target_id for e in graph["edges"])
    citations = client.get(f"/api/papers/{target_id}/citations").json()["items"]
    assert len(citations) == 1
    (papers / "b.pdf").write_bytes(pdf_bytes("Robust Camera Tracking", "10.9999/tracking.b", references=False))
    register(client, settings, "b.pdf")
    run_jobs(app)
    assert client.get(f"/api/papers/{target_id}").json()["has_pdf"]
    assert client.get("/api/papers").json()["total"] == 2
    latest = client.get(f"/api/papers/{source['id']}").json()
    response = client.patch(f"/api/papers/{source['id']}", json={"fields": {"title": "人工修订的标题"}, "expected_version": latest["row_version"]})
    assert response.status_code == 200
    assert client.patch(f"/api/papers/{source['id']}", json={"fields": {"title": "stale"}, "expected_version": latest["row_version"]}).status_code == 409
    assert client.post(f"/api/files/{imported['file_id']}/parse", json={"force": True}).status_code == 202
    run_jobs(app)
    assert client.get(f"/api/papers/{source['id']}").json()["title"] == "人工修订的标题"
    export = client.get("/api/exports/csl-json")
    assert export.status_code == 200 and "canonical_uri" not in export.text


def test_settings_persist_and_new_app_retains_file_identity(environment):
    app, client, papers, settings = environment
    (papers / "a.pdf").write_bytes(pdf_bytes())
    imported = register(client, settings, "a.pdf")
    reloaded = Settings.load(data_dir=settings.data_dir)
    assert reloaded.host_id == settings.host_id
    app2 = create_app(reloaded, run_worker=False)
    with TestClient(app2, base_url="http://127.0.0.1:8765") as second:
        second.get("/api/session")
        assert second.get(f"/api/files/{imported['file_id']}/content").status_code == 200

def test_bulk_import_deduplicates_identical_content_and_jobs(environment):
    app, client, papers, settings = environment
    original = pdf_bytes()
    (papers / "first.pdf").write_bytes(original)
    (papers / "different-name.pdf").write_bytes(original)
    (papers / "another-paper.pdf").write_bytes(pdf_bytes("New Camera Paper", "10.9999/new.paper"))
    payload = {
        "root_id": settings.roots()[0]["id"],
        "paths": ["first.pdf", "different-name.pdf", "another-paper.pdf", "different-name.pdf"],
    }

    first = client.post("/api/files/register", json=payload)
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["errors"] == []
    assert result["summary"] == {"added": 2, "duplicates": 2, "failed": 0}
    assert len(result["items"]) == 3  # Repeated relative path is processed once.
    a, duplicate, b = result["items"]
    assert a["created"] is True and a["job_id"]
    assert duplicate["created"] is False and duplicate["duplicate"] is True
    assert duplicate["job_id"] is None
    assert b["created"] is True and b["job_id"]
    assert (a["file_id"], a["paper_id"]) == (duplicate["file_id"], duplicate["paper_id"])
    assert len(app.state.store.list_jobs()) == 2
    assert app.state.store.list_papers()["total"] == 2

    # A subsequent import registers no new paper, asset or automatic parse job.
    second = client.post("/api/files/register", json=payload)
    assert second.status_code == 202, second.text
    assert second.json()["summary"] == {"added": 0, "duplicates": 4, "failed": 0}
    assert all(not item["created"] and item["job_id"] is None for item in second.json()["items"])
    assert len(app.state.store.list_jobs()) == 2
    assert app.state.store.list_papers()["total"] == 2
    asset = app.state.store.get_asset(a["file_id"])
    assert len([p for p in asset["locations"] if p["is_current"]]) == 2


def test_upload_deduplicates_same_pdf_under_different_names(environment):
    app, client, papers, settings = environment
    data = pdf_bytes()
    first = client.post("/api/files/upload", files=[
        ("files", ("one.pdf", data, "application/pdf")),
        ("files", ("two.pdf", data, "application/pdf")),
    ])
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["summary"] == {"added": 1, "duplicates": 1, "failed": 0}
    a, duplicate = result["items"]
    assert a["created"] and a["job_id"]
    assert not duplicate["created"] and duplicate["job_id"] is None
    assert a["file_id"] == duplicate["file_id"]
    assert a["paper_id"] == duplicate["paper_id"]
    assert len(list((settings.data_dir / "managed").glob("*.pdf"))) == 1

    again = client.post("/api/files/upload", files=[("files", ("three.pdf", data, "application/pdf"))])
    assert again.status_code == 202
    assert again.json()["summary"] == {"added": 0, "duplicates": 1, "failed": 0}
    assert len(app.state.store.list_jobs()) == 1
    assert app.state.store.list_papers()["total"] == 1


def test_upload_matching_linked_pdf_reuses_paper_but_adds_managed_location(environment):
    app, client, papers, settings = environment
    data = pdf_bytes()
    (papers / "original.pdf").write_bytes(data)
    linked = register(client, settings, "original.pdf")
    response = client.post("/api/files/upload", files=[("files", ("backup.pdf", data, "application/pdf"))])
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["summary"] == {"added": 0, "duplicates": 1, "failed": 0}
    uploaded = result["items"][0]
    assert uploaded["file_id"] == linked["file_id"]
    assert uploaded["paper_id"] == linked["paper_id"]
    assert uploaded["job_id"] is None
    assert app.state.store.list_papers()["total"] == 1
    assert len(app.state.store.list_jobs()) == 1
    locations = app.state.store.get_asset(linked["file_id"])["locations"]
    assert {loc["location_kind"] for loc in locations if loc["is_current"]} == {"linked", "managed"}


def test_import_dialog_has_select_all_control(environment):
    app, client, papers, settings = environment
    html = client.get("/").text
    assert 'id="browse-select-all"' in html
    assert 'aria-label="全选当前目录的 PDF"' in html
    assert 'id="browse-up"' in html
