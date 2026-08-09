"""
Structural non-logging guarantee.

The privacy story for Squire depends on ingress being *structurally*
incapable of logging Telegram message content -- not just "we promise not
to". This module is the ONLY place in the service allowed to call the
stdlib logger. Its single public function, log_event(), has a closed
parameter list of safe, structural fields:

    timestamp, event, bot_id, tenant_id, status_code, latency_ms, queue_depth

There is no `body`, `update`, `headers`, or `payload` parameter -- so no
call site anywhere in the codebase can pass message content through this
function even by accident. If you're auditing this claim: grep the repo for
`log_event(` and confirm every call site only supplies fields from that
list, and grep for `logging.getLogger` / `print(` to confirm nothing else
emits logs.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("ingress")


def log_event(
    event: str,
    *,
    bot_id: Optional[int] = None,
    tenant_id: Optional[str] = None,
    status_code: Optional[int] = None,
    latency_ms: Optional[float] = None,
    queue_depth: Optional[int] = None,
) -> None:
    """Emit one structured JSON log line. Fixed field set -- see module docstring."""
    record = {
        "timestamp": time.time(),
        "event": event,
        "bot_id": bot_id,
        "tenant_id": tenant_id,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "queue_depth": queue_depth,
    }
    logger.info(json.dumps(record))
