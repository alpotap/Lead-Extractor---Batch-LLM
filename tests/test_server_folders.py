from typing import Any


def test_folder_api_creates_and_activates_isolated_workspace(client: Any, temp_data_root):
    response = client.post("/api/folders", json={"name": "Campaign B"})
    assert response.status_code == 200, response.text
    folder = response.json()["folder"]
    assert folder["name"] == "Campaign B"

    listing = client.get("/api/folders").json()
    assert listing["active_folder_id"] == folder["id"]
    assert listing["active_folder_name"] == "Campaign B"

    active_root = client.get("/api/settings").json()["resolved_root"]
    assert active_root.endswith(f"workspaces/{folder['id']}") or active_root.endswith(f"workspaces\\{folder['id']}")

    response = client.post("/api/folders/activate", json={"id": ""})
    assert response.status_code == 200
    assert client.get("/api/settings").json()["resolved_root"] == str(temp_data_root)


def test_folder_can_be_renamed_without_moving_its_data(client: Any):
    folder = client.post("/api/folders", json={"name": "Before"}).json()["folder"]
    response = client.post("/api/folders/rename", json={"id": folder["id"], "name": "After"})
    assert response.status_code == 200, response.text
    assert response.json()["folder"] == {"id": folder["id"], "name": "After"}
    active = client.get("/api/folders").json()
    assert active["active_folder_name"] == "After"


def test_folder_with_data_requires_name_confirmation_before_delete(client: Any):
    import server

    folder = client.post("/api/folders", json={"name": "Delete Me"}).json()["folder"]
    workspace_root = server._get_root()
    (workspace_root / "companies" / "sample.json").write_text("{}", encoding="utf-8")

    summary = client.get(f"/api/folders/{folder['id']}/summary").json()
    assert summary["has_data"] is True
    assert summary["file_count"] == 1

    rejected = client.post("/api/folders/delete", json={"id": folder["id"], "confirmed": True})
    assert rejected.status_code == 409
    assert (workspace_root / "companies" / "sample.json").exists()

    deleted = client.post(
        "/api/folders/delete",
        json={"id": folder["id"], "confirmed": True, "confirmation": "Delete Me"},
    )
    assert deleted.status_code == 200, deleted.text
    assert not workspace_root.exists()
    assert client.get("/api/folders").json()["active_folder_id"] == ""




def test_default_folder_can_be_renamed(client: Any):
    response = client.post("/api/folders/rename", json={"id": "", "name": "My Leads"})
    assert response.status_code == 200, response.text
    assert client.get("/api/folders").json()["folders"][0] == {"id": "", "name": "My Leads"}
    duplicate = client.post("/api/folders", json={"name": "my leads"})
    assert duplicate.status_code == 400


def test_active_folder_survives_settings_reload_and_remains_in_dropdown(client: Any):
    import server

    folder = client.post("/api/folders", json={"name": "Resume Me"}).json()["folder"]
    restarted_settings = server.load_settings()
    assert restarted_settings["active_folder_id"] == folder["id"]
    listing = client.get("/api/folders").json()
    assert listing["active_folder_id"] == folder["id"]
    assert folder in listing["folders"]
