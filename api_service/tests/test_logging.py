"""Regression check for persistent structured logging."""

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from api_service import logging_config


def run() -> None:
    with TemporaryDirectory() as directory:
        logging_config.LOG_FILE = Path(directory) / "system.jsonl"
        logging_config.LOG_MAX_BYTES = 1024 * 1024
        logging_config.LOG_BACKUP_COUNT = 2
        logging_config._configured = False
        log_path = logging_config.configure_logging()
        token = logging_config.bind_request_id("request-test")
        try:
            logging_config.get_logger("test").info(
                "Task reached parsing stage",
                extra={
                    "job_id": "job-test",
                    "document_name": "sample.pdf",
                    "progress": 42,
                    "stage": "parsing",
                },
            )
        finally:
            logging_config.reset_request_id(token)
        for handler in logging.getLogger("eplan").handlers:
            handler.flush()

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["level"] == "INFO"
        assert event["component"] == "test"
        assert event["request_id"] == "request-test"
        assert event["job_id"] == "job-test"
        assert event["document_name"] == "sample.pdf"
        assert event["progress"] == 42
        assert event["stage"] == "parsing"


if __name__ == "__main__":
    run()
    print("structured logging test passed")
