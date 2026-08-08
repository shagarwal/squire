#!/usr/bin/env python3
"""Provision one Squire tenant. **This is the entire alpha signup path.**

    export CONTROL_API_URL=https://control-api.squire.app
    export INTERNAL_API_TOKEN=...
    python infra/provision.py --email alpha@example.com

Creates the tenant, drives the provisioning state machine to completion, and prints
the `t.me/<bot>` link the user taps to meet their agent.

Exit codes:  0 = tenant live   1 = provisioning failed   2 = timed out
"""

from __future__ import annotations

import argparse
import sys
import time

from _control_api_client import ControlAPI, ControlAPIError, fail

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL = 2.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a Squire tenant.")
    parser.add_argument("--email", required=True, help="tenant's signup email")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="give up after this many seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds between state-machine ticks (default: %(default)s)",
    )
    parser.add_argument(
        "--control-api-url", default=None, help="overrides $CONTROL_API_URL"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        api = ControlAPI(base_url=args.control_api_url)
    except ControlAPIError as exc:
        return fail(str(exc))

    # --- 1. Register the tenant (idempotent per email) -----------------------
    response = api.post("/internal/tenants", json={"email": args.email})
    if response.status_code >= 400:
        detail = _detail(response)
        return fail(f"could not create tenant: {detail}")

    created = response.json()
    tenant_id = created["tenant_id"]
    job_id = created["job_id"]
    print(f"tenant   {tenant_id}  ({args.email})")
    print(f"bot      @{created.get('bot_username')}")
    print(f"job      {job_id}")
    print("provisioning...")

    # --- 2. Drive the state machine ----------------------------------------
    # We call `advance` rather than just polling `GET job`: it is idempotent, it
    # bypasses retry backoff, and it means provisioning still completes even if the
    # control-api background worker is disabled.
    deadline = time.monotonic() + args.timeout
    while True:
        response = api.post(f"/internal/provision-jobs/{job_id}/advance")
        if response.status_code >= 400:
            return fail(f"advance failed: {_detail(response)}")
        job = response.json()
        status = job["status"]

        if status == "done":
            break
        if status == "failed":
            print(f"  step {job['step']}: FAILED")
            return fail(job.get("last_error") or "provisioning failed")

        print(f"  step {job['step']}... ({status})")
        if time.monotonic() >= deadline:
            print(f"  last step: {job['step']}")
            print("ERROR: timed out waiting for provisioning to finish")
            return 2
        if args.poll_interval:
            time.sleep(args.poll_interval)

    # --- 3. Report ----------------------------------------------------------
    tenant = api.get(f"/internal/tenants/{tenant_id}").json()
    link = tenant.get("telegram_link") or created.get("telegram_link")
    print()
    print(f"status   {tenant.get('status')}")
    print(f"OPEN     {link}")
    print()
    print("Tap Start in Telegram; the agent greets the user and runs onboarding.")
    return 0


def _detail(response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except Exception:  # noqa: BLE001
        return response.text[:300]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
