import os
from pathlib import Path

from services.ollama import _raw_log_event
from services.pipeline import close_data_loggers, configure_logging


def test_restore_can_move_workspace_after_file_loggers_are_closed(tmp_path: Path):
    root = tmp_path / "workspace"
    log_dir = root / "logs"
    log_dir.mkdir(parents=True)
    configure_logging(root).info("test before restore")
    _raw_log_event(
        log_dir / "llm_raw.jsonl",
        {"event": "test"},
        10_000,
        1,
    )

    close_data_loggers()
    os.replace(root, tmp_path / "old-workspace")

    assert (tmp_path / "old-workspace" / "logs" / "application.log").exists()
    assert (tmp_path / "old-workspace" / "logs" / "llm_raw.jsonl").exists()
