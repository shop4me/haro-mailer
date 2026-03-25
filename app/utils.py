import json
import logging
from datetime import datetime
from typing import Any


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    )


def now_utc() -> datetime:
    return datetime.utcnow()


def safe_json_dumps(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=True)
    except Exception:
        return "{}"


def safe_json_loads(raw: str, default: Any):
    try:
        return json.loads(raw)
    except Exception:
        return default
