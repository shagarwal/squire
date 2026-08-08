"""Tiny shared control-api client for the operator CLIs.

Deliberately dependency-light (httpx only) so `infra/` scripts can be run from a
laptop with `uv run` without installing the control-api package.
"""

from __future__ import annotations

import os
import sys

import httpx

DEFAULT_TIMEOUT = 30.0


class ControlAPIError(RuntimeError):
    pass


class ControlAPI:
    """Authenticated wrapper over control-api's `/internal/*` endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or os.environ.get("CONTROL_API_URL", "")).rstrip("/")
        self.token = token or os.environ.get("INTERNAL_API_TOKEN", "")
        if not self.base_url:
            raise ControlAPIError("CONTROL_API_URL is not set")
        if not self.token:
            raise ControlAPIError("INTERNAL_API_TOKEN is not set")
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        with httpx.Client(timeout=self.timeout) as client:
            return client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                **kwargs,
            )

    def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._request("POST", path, json=json)

    def get(self, path: str) -> httpx.Response:
        return self._request("GET", path)


def fail(message: str) -> int:
    """Print an error to stdout (so capsys/CI logs catch it) and return exit code 1."""
    print(f"ERROR: {message}")
    sys.stdout.flush()
    return 1
