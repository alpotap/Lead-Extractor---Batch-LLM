from services import ollama


def test_outgoing_llm_request_is_logged_before_response(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama, "_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(
        ollama,
        "_request",
        lambda *args, **kwargs: {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]
        },
    )
    events = []
    monkeypatch.setattr(
        ollama,
        "_raw_log_event",
        lambda path, event, max_bytes, backups: events.append(event.copy()),
    )

    ollama._generate_json(
        "https://api.example/v1", "test-model", "full prompt text",
        source_file="test.txt", task="full_extraction", segment=1, segment_count=1,
        raw_log_path=tmp_path / "llm_raw.jsonl", raw_log_max_bytes=100_000, raw_log_backups=1,
    )

    outgoing = events[0]
    assert outgoing["event"] == "request_sent"
    assert outgoing["request"]["url"] == "https://api.example/v1/chat/completions"
    assert outgoing["request"]["payload"]["messages"][0]["content"] == "full prompt text"
    assert events[1]["event"] == "extraction_response"
