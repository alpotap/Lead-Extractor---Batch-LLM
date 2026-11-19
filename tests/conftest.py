"""Shared test fixtures for Lead Extractor tests.

All fixtures point the app at a temporary data directory and a temporary
settings file so tests never mutate the user's real config/settings.json
or real data directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server  # noqa: E402  (needs project root on sys.path)
from services.storage import DEFAULT_SETTINGS, save_settings, SETTINGS_PATH


@pytest.fixture
def temp_settings_path(tmp_path: Path) -> Path:
    """A temporary settings file path so save_settings() never touches the real config/settings.json."""
    return tmp_path / "settings.json"


@pytest.fixture
def temp_data_root(tmp_path: Path) -> Path:
    """Create a temp data root directory with the expected folder structure."""
    root = tmp_path / "leaddata"
    for folder in (
        "ingest/incoming",
        "ingest/archived",
        "ingest/parsed",
        "extracted",
        "companies",
        "crm",
        "logs",
        "backups",
        "review",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def client(
    temp_data_root: Path,
    temp_settings_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """FastAPI TestClient with settings pointing at the temp data root.

    Monkeypatches services.storage.SETTINGS_PATH so save_settings() writes to
    a temp file instead of the user's real config/settings.json. Also patches
    server._get_settings to load from that temp file.
    """
    import services.storage as storage

    # Point SETTINGS_PATH at a temp file so save_settings() never touches the real settings.json.
    monkeypatch.setattr(storage, "SETTINGS_PATH", temp_settings_path)

    # Write test settings to the temp file.
    test_settings = dict(DEFAULT_SETTINGS)
    test_settings["data_path"] = str(temp_data_root)
    save_settings(test_settings)

    # Make server._get_settings return our temp settings (reads from temp SETTINGS_PATH).
    monkeypatch.setattr(server, "_get_settings", lambda: server.load_settings())

    # Invalidate module-level caches so each test starts fresh with its own temp data root.
    server._invalidate_caches()

    return TestClient(server.app)
