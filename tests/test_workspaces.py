from services.storage import create_data_folder, data_root, list_data_folders


def test_workspaces_have_independent_data_roots(tmp_path):
    settings = {"data_path": str(tmp_path), "active_folder_id": ""}
    default_root = data_root(settings)

    folder = create_data_folder(settings, "Partner Leads")
    settings["active_folder_id"] = folder["id"]
    partner_root = data_root(settings)

    assert partner_root != default_root
    assert partner_root == tmp_path / "workspaces" / folder["id"]
    assert {item["name"] for item in list_data_folders(settings)} == {"Default", "Partner Leads"}
    assert (default_root / "companies").exists()
    assert (partner_root / "review").exists()


def test_duplicate_workspace_names_are_rejected(tmp_path):
    settings = {"data_path": str(tmp_path), "active_folder_id": ""}
    create_data_folder(settings, "Customers")
    try:
        create_data_folder(settings, " customers ")
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("Duplicate workspace name was accepted")
