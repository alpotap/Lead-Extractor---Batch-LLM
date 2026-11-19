import json
import io
from pathlib import Path
from zipfile import ZipFile
import time

import pytest

from services.backups import create_data_archive, restore_data_archive
from services.storage import create_data_folder, data_root


def test_archive_contains_visible_workspace_data_and_excludes_processing_caches(tmp_path: Path):
    base = tmp_path / "LeadData"
    settings = {"data_path": str(base), "active_folder_id": "", "custom_statuses": ["Prospect", "Nurture"]}
    settings["default_folder_name"] = "Prospects"
    default = data_root(settings)
    folder = create_data_folder(settings, "Client B")
    settings["active_folder_id"] = folder["id"]
    workspace = data_root(settings)

    (default / "companies" / "alpha.json").write_text(
        '{"company_name":"Alpha","sources":["source.html"]}', encoding="utf-8"
    )
    (default / "crm" / "alpha.json").write_text('{"status":"Customer"}', encoding="utf-8")
    (default / "categories.json").write_text('{"categories":{"Health":[]}}', encoding="utf-8")
    (default / "review" / "dismissed").mkdir(parents=True, exist_ok=True)
    (default / "review" / "failed.review.json").write_text('{"reason":"error"}', encoding="utf-8")
    (default / "review" / "dismissed" / "old.review.json").write_text('{"reason":"old"}', encoding="utf-8")
    (default / "ingest" / "archived" / "source.html").write_text("<h1>Source</h1>", encoding="utf-8")
    (default / "ingest" / "incoming" / "queued.md").write_text("Queued source", encoding="utf-8")
    (default / "ingest" / "parsed" / "source.html.parsed.txt").write_text("cache", encoding="utf-8")
    (default / "extracted" / "source.extracted.json").write_text("{}", encoding="utf-8")
    (default / "logs" / "application.log").write_text("log", encoding="utf-8")
    (workspace / "companies" / "beta.json").write_text('{"company_name":"Beta"}', encoding="utf-8")

    archive_path = tmp_path / "backup.zip"
    progress = []
    create_data_archive(
        settings,
        archive_path,
        progress_callback=lambda *values: progress.append(values),
    )
    assert progress[0][0:2] == (0, progress[-1][1])
    assert progress[-1][0] == progress[-1][1]
    assert progress[-1][2] == progress[-1][3]

    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "archive_manifest.json" in names
        manifest = json.loads(archive.read("archive_manifest.json"))
        assert manifest["format"] == "lead-extractor-data-v2"
        assert manifest["folders"][0]["name"] == "Prospects"
        assert manifest["custom_statuses"] == ["Prospect", "Nurture"]
        assert "data/default/companies/alpha.json" in names
        assert "data/default/review/dismissed/old.review.json" in names
        assert f"data/workspaces/{folder['id']}/companies/beta.json" in names
        assert not any("/ingest/" in name or "/parsed/" in name or "/extracted/" in name or "/logs/" in name for name in names)

    (default / "companies" / "alpha.json").write_text('{"company_name":"Changed"}', encoding="utf-8")
    restored_state = restore_data_archive(settings, archive_path)
    assert restored_state["active_folder_id"] == folder["id"]
    assert restored_state["default_folder_name"] == "Prospects"
    assert restored_state["custom_statuses"] == ["Prospect", "Nurture"]
    assert json.loads((default / "companies" / "alpha.json").read_text(encoding="utf-8"))["company_name"] == "Alpha"
    assert json.loads((default / "companies" / "alpha.json").read_text(encoding="utf-8"))["sources"] == ["source.html"]
    assert not (default / "ingest").exists()
    assert not (default / "extracted").exists()
    assert not (default / "logs").exists()
    assert (workspace / "companies" / "beta.json").exists()


def test_restore_rejects_traversal_archive_without_modifying_data(tmp_path: Path):
    base = tmp_path / "LeadData"
    root = data_root({"data_path": str(base), "active_folder_id": ""})
    company = root / "companies" / "keep.json"
    company.write_text("keep", encoding="utf-8")
    archive_path = tmp_path / "unsafe.zip"
    manifest = {
        "format": "lead-extractor-data-v1",
        "active_folder_id": "",
        "folders": [{"id": "", "name": "Default"}],
    }
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("archive_manifest.json", json.dumps(manifest))
        archive.writestr("../escape.json", "unsafe")

    with pytest.raises(ValueError, match="unsafe path"):
        restore_data_archive({"data_path": str(base), "active_folder_id": ""}, archive_path)
    assert company.read_text(encoding="utf-8") == "keep"


def test_legacy_archive_drops_original_sources_during_restore(tmp_path: Path):
    base = tmp_path / "LeadData"
    archive_path = tmp_path / "legacy.zip"
    manifest = {
        "format": "lead-extractor-data-v1",
        "active_folder_id": "",
        "folders": [{"id": "", "name": "Default"}],
    }
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("archive_manifest.json", json.dumps(manifest))
        archive.writestr("data/default/companies/legacy.json", '{"company_id":"legacy"}')
        archive.writestr("data/default/ingest/archived/old-page.html", "large source")

    restored = restore_data_archive({"data_path": str(base)}, archive_path)
    assert restored["active_folder_id"] == ""
    assert (base / "companies" / "legacy.json").exists()
    assert not (base / "ingest").exists()


def test_archive_job_reports_progress_and_serves_download(client):
    created = client.post("/api/companies", json={"company_name": "Archive Progress Co"})
    assert created.status_code == 200

    started = client.post("/api/data/archive/jobs")
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    for _ in range(100):
        job_res = client.get(f"/api/data/archive/jobs/{job_id}")
        assert job_res.status_code == 200
        job = job_res.json()
        if job["finished"]:
            break
        time.sleep(0.02)

    assert job["status"] == "ready"
    assert job["total_files"] >= 1
    assert job["files_done"] == job["total_files"]
    downloaded = client.get(f"/api/data/archive/jobs/{job_id}/download")
    assert downloaded.status_code == 200
    with ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert "archive_manifest.json" in archive.namelist()


def test_archive_restore_pauses_and_resumes_source_watcher(client, tmp_path, monkeypatch):
    import server

    settings = server._get_settings()
    root = server._get_root()
    (root / "companies" / "restore-me.json").write_text(
        '{"company_id":"restore-me","company_name":"Restore Me"}', encoding="utf-8"
    )
    archive_path = tmp_path / "restore-me.zip"
    create_data_archive(settings, archive_path)
    paused_tokens = []
    resumed_tokens = []

    def pause(timeout_seconds):
        token = True
        paused_tokens.append(token)
        return token

    def resume(token):
        resumed_tokens.append(token)

    monkeypatch.setattr(server, "pause_background_scanner", pause)
    monkeypatch.setattr(server, "resume_background_scanner", resume)
    response = client.post(
        "/api/data/archive/restore",
        content=archive_path.read_bytes(),
        headers={"Content-Type": "application/zip", "X-Restore-Confirm": "RESTORE"},
    )

    assert response.status_code == 200, response.text
    assert paused_tokens == [True]
    assert resumed_tokens == [True]
