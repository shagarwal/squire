"""
Smoke test for the app's lifespan (startup/shutdown of the background retry
worker task). The functional test_webhook_endpoint.py tests bypass lifespan
(ASGITransport doesn't trigger it) and drive retry_once() directly for
determinism, so this test's only job is to confirm the real
create_app()/lifespan wiring in ingress.app doesn't blow up when a server
actually starts and stops it -- i.e. that `uvicorn ingress.main:app` would
boot cleanly.
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from ingress.app import create_app


def test_app_starts_and_stops_cleanly(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)  # doesn't matter, no requests made in this test

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings=settings, client=mock_client)

    # Entering/exiting the context manager runs the lifespan startup and
    # shutdown handlers, including creating and cancelling the background
    # retry task -- if that wiring were broken this would hang or raise.
    with TestClient(app):
        pass
