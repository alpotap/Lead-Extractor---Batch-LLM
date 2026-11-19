"""Tests for the ollama.py _RAW_LOGGERS_LOCK fix.

Regression test for: 'builtin_function_or_method' object does not support
the context manager protocol. The lock must be an instance of threading.Lock,
not the threading.Lock class itself.
"""

from __future__ import annotations

import threading

import services.ollama as ollama


def test_raw_loggers_lock_is_instance_not_class() -> None:
    """_RAW_LOGGERS_LOCK must be a lock instance, not the Lock class/function."""
    assert ollama._RAW_LOGGERS_LOCK is not threading.Lock
    assert isinstance(ollama._RAW_LOGGERS_LOCK, type(threading.Lock()))


def test_raw_loggers_lock_supports_context_manager() -> None:
    """The lock must work with the 'with' statement (context manager protocol)."""
    acquired = False
    with ollama._RAW_LOGGERS_LOCK:
        acquired = True
    assert acquired is True


def test_raw_loggers_lock_can_be_acquired_and_released() -> None:
    """Basic acquire/release round-trip must work."""
    ollama._RAW_LOGGERS_LOCK.acquire()
    try:
        assert ollama._RAW_LOGGERS_LOCK.locked()
    finally:
        ollama._RAW_LOGGERS_LOCK.release()
    assert not ollama._RAW_LOGGERS_LOCK.locked()
