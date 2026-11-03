"""Client SDKs must never log request payloads (document text) in prod."""
import logging

import app.main  # noqa: F401  (configures logging on import)


def test_root_logger_is_not_debug():
    assert logging.getLogger().getEffectiveLevel() >= logging.INFO


def test_payload_logging_libraries_are_quiet():
    for name in ("voyage", "voyageai", "anthropic", "httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
    assert not logging.getLogger("voyage").isEnabledFor(logging.DEBUG)
