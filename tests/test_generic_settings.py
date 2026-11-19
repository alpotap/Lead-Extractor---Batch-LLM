import json

from services import storage


def test_settings_migrate_legacy_ollama_fields_to_generic_api_fields(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    secrets_path = tmp_path / "secrets.json"
    settings_path.write_text(json.dumps({"ollama_url": "https://api.example.com/v1"}), encoding="utf-8")
    secrets_path.write_text(json.dumps({"ollama_api_key": "secret"}), encoding="utf-8")
    monkeypatch.setattr(storage, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(storage, "SECRETS_PATH", secrets_path)

    settings = storage.load_settings()
    assert settings["api_url"] == "https://api.example.com/v1"
    assert settings["ollama_url"] == settings["api_url"]
    assert settings["api_key"] == "secret"

    settings["api_url"] = "https://new.example.com/v1"
    settings["ollama_url"] = settings["api_url"]
    storage.save_settings(settings)
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["api_url"] == "https://new.example.com/v1"
    assert "ollama_url" not in saved
    assert "api_key" not in saved and "ollama_api_key" not in saved
