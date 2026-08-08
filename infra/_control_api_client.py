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
        """Perform one authenticated request.

        Transport failures are re-raised as `ControlAPIError` rather than escaping
        as raw httpx exceptions. The CLIs poll for minutes at a time (the upgrade
        drill waits up to 600s per tenant), so a single transient ConnectError or
        ReadTimeout is close to expected -- and letting it propagate would kill the
        process mid-roll with a traceback, discarding the drill's failure list and
        its accounting of which tenants were left untouched. Callers already handle
        `ControlAPIError`; they cannot handle a stack unwind.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                return client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            # `type(exc).__name__` because httpx's message for a timeout is often
            # empty, and "ReadTimeout" alone is the useful half of the diagnosis.
            raise ControlAPIError(
                f"{method} {path} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return self._request("POST", path, json=json)

    def get(self, path: str) -> httpx.Response:
        return self._request("GET", path)


def fail(message: str) -> int:
    """Print an error to stdout (so capsys/CI logs catch it) and return exit code 1."""
    print(f"ERROR: {message}")
    sys.stdout.flush()
    return 1
