from collections import deque

import services.ollama as ollama


def test_llm_requests_are_capped_at_40_per_rolling_minute(monkeypatch):
    now = [100.0]
    sleeps = []

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(ollama.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ollama.time, "sleep", fake_sleep)
    monkeypatch.setattr(ollama, "_request_times", deque())

    for _ in range(40):
        ollama._wait_for_request_slot()
    assert len(ollama._request_times) == 40
    assert sleeps == []

    ollama._wait_for_request_slot()
    assert sleeps == [60.0]
    assert len(ollama._request_times) == 1
