import threading
import time

from models.lead import EXTRACTION_TEMPLATE
from services import ollama


def test_large_source_chunks_are_requested_in_parallel(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak_active = 0
    calls = 0

    def fake_generate(*args, **kwargs):
        nonlocal active, peak_active, calls
        with lock:
            active += 1
            calls += 1
            peak_active = max(peak_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {key: ([] if isinstance(value, list) else value) for key, value in EXTRACTION_TEMPLATE.items()}

    monkeypatch.setattr(ollama, "_generate_json", fake_generate)
    ollama.extract_lead("https://api.example/v1", "model", "x" * 18000)

    assert calls == 3
    assert 1 < peak_active <= ollama.MAX_PARALLEL_LLM_REQUESTS
