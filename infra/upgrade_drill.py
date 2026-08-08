#!/usr/bin/env python3
"""Roll the tenant fleet onto a new image, canary first. **Task 0.6's upgrade drill.**

    export CONTROL_API_URL=https://control-api.squire.app
    export INTERNAL_API_TOKEN=...

    # 1. canary one tenant and verify it came back on the new image
    python infra/upgrade_drill.py --image-tag v2 --canary t-abc123

    # 2. same command plus --roll: canary again (skipped -- already converged),
    #    then the rest of the fleet, one at a time, aborting on repeated failure
    python infra/upgrade_drill.py --image-tag v2 --canary t-abc123 --roll

    # 3. rollback: it is just a redeploy of the previous reference
    python infra/upgrade_drill.py --image-tag v1 --rollback

WHAT "VERIFIED" MEANS HERE
-------------------------
Railway reporting a successful deploy is not evidence that a tenant is *working* --
the container can come up, fail its DEK gate, and sit there. So a tenant counts as
converged only when `GET /internal/fleet` shows BOTH:

  * a fresh heartbeat (the container is alive and talking to control-api), AND
  * `reported_image_ref == the reference we asked for` (it is the NEW container
    talking, not the old one whose last beat is still inside the staleness window).

The second half is why `POST /internal/tenants/{id}/redeploy` writes SQUIRE_IMAGE_REF
onto the service before deploying: without it there is no way to distinguish "rolled"
from "we asked, and something happened".

Exit codes:  0 = every targeted tenant converged   1 = one or more failed
             2 = timed out waiting for a tenant to come back
"""

from __future__ import annotations

import argparse
import sys
import time

from _control_api_client import ControlAPI, ControlAPIError, fail

#: How long to wait for one tenant to come back on the new image. Generous on
#: purpose: it covers a Railway image pull, the tenant's own boot (hindsight's
#: initdb on a cold volume is the slow part) and the first heartbeat.
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_POLL_INTERVAL = 10.0
#: Abort the roll after this many tenants fail to converge. 1 by default: if the
#: canary passed and the very next tenant does not, the safe assumption is that the
#: image is bad for some subset of the fleet, and the operator should look before
#: another 300 tenants are restarted onto it.
DEFAULT_MAX_FAILURES = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canary + roll the Squire tenant fleet onto an image tag."
    )
    parser.add_argument(
        "--image-tag",
        required=True,
        help="bare tag (v2) or a full reference (ghcr.io/org/img:v2)",
    )
    parser.add_argument("--canary", default=None, help="tenant id to roll first")
    parser.add_argument(
        "--roll",
        action="store_true",
        help="after the canary, roll the rest of the running fleet",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="roll the whole running fleet onto --image-tag with no canary gate "
        "and no abort threshold (this is what an emergency looks like)",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=DEFAULT_MAX_FAILURES,
        help="abort the roll after this many failures (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-tenant convergence timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds between /fleet polls (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit without redeploying anything",
    )
    parser.add_argument("--control-api-url", default=None, help="overrides $CONTROL_API_URL")
    args = parser.parse_args(argv)

    if not (args.canary or args.roll or args.rollback):
        parser.error("nothing to do: pass --canary, --roll and/or --rollback")
    # Both must be strictly positive. `--poll-interval 0` turns the convergence
    # wait into a busy loop hammering /fleet for the whole timeout, and
    # `--timeout 0` makes every tenant fail verification instantly, which on a
    # roll reads as "the new image is broken" when in fact nothing was waited for.
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


# ---------------------------------------------------------------------------
# control-api helpers
# ---------------------------------------------------------------------------


def fleet(api: ControlAPI, status: str | None = "running") -> dict[str, dict]:
    """`GET /internal/fleet` as {tenant_id: row}. Raises on a non-200."""
    path = "/internal/fleet" + (f"?status={status}" if status else "")
    response = api.get(path)
    if response.status_code >= 400:
        raise ControlAPIError(f"GET {path} -> {response.status_code}: {_detail(response)}")
    return {row["tenant_id"]: row for row in response.json()["tenants"]}


def redeploy(api: ControlAPI, tenant_id: str, image_tag: str) -> str:
    """Trigger the redeploy; returns the fully-resolved image reference."""
    response = api.post(
        f"/internal/tenants/{tenant_id}/redeploy", json={"image_tag": image_tag}
    )
    if response.status_code >= 400:
        raise ControlAPIError(f"redeploy {tenant_id} failed: {_detail(response)}")
    return response.json()["image_ref"]


def converged(row: dict | None, image_ref: str) -> bool:
    """Fresh heartbeat AND the tenant itself says it is on the target image."""
    if not row:
        return False
    return bool(row.get("heartbeat_fresh")) and row.get("reported_image_ref") == image_ref


def already_on(row: dict | None, image_tag: str) -> bool:
    """True when a tenant is already alive on the requested image.

    Compares against what the tenant REPORTS, and accepts either form of
    `--image-tag`: a full reference (equality) or a bare tag (suffix match on the
    reference's tag). This is what makes the whole drill re-runnable -- after an
    abort, the same command picks up where it stopped instead of restarting
    containers that are already fine.
    """
    if not row or not row.get("heartbeat_fresh"):
        return False
    reported = row.get("reported_image_ref") or ""
    return reported == image_tag or reported.endswith(f":{image_tag}")


def wait_for(
    api: ControlAPI,
    tenant_id: str,
    image_ref: str,
    timeout: float,
    poll_interval: float,
) -> bool:
    """Poll /fleet until the tenant converges or the timeout expires."""
    deadline = time.monotonic() + timeout
    while True:
        row = fleet(api, status=None).get(tenant_id)
        if converged(row, image_ref):
            return True
        if time.monotonic() >= deadline:
            age = (row or {}).get("heartbeat_age_seconds")
            reported = (row or {}).get("reported_image_ref")
            print(
                f"  {tenant_id}: NOT CONVERGED "
                f"(reported={reported!r} want={image_ref!r} heartbeat_age={age})"
            )
            return False
        time.sleep(poll_interval)  # parse_args guarantees this is > 0


def roll_one(api: ControlAPI, tenant_id: str, args) -> bool:
    """Redeploy + verify one tenant. Returns True when it converged.

    Never raises. A control-api failure -- at redeploy time OR during the minutes
    of polling that follow -- is reported as "this tenant did not converge" and
    handed back to the caller, which owns the abort decision. Letting a transient
    error escape here would end the process mid-roll and throw away both the
    failure list and the count of tenants still untouched, which is precisely the
    information an operator needs after an aborted upgrade.
    """
    if args.dry_run:
        print(f"  {tenant_id}: would redeploy onto {args.image_tag}")
        return True
    try:
        image_ref = redeploy(api, tenant_id, args.image_tag)
    except ControlAPIError as exc:
        print(f"  {tenant_id}: FAILED to redeploy -- {exc}")
        return False

    print(f"  {tenant_id}: redeployed onto {image_ref}, waiting for a heartbeat...")
    try:
        ok = wait_for(api, tenant_id, image_ref, args.timeout, args.poll_interval)
    except ControlAPIError as exc:
        # The deploy was already triggered, so this tenant is in an unknown state:
        # count it as not-converged and let the threshold logic stop the roll.
        print(f"  {tenant_id}: FAILED to verify -- {exc}")
        return False

    if ok:
        print(f"  {tenant_id}: OK")
        return True
    return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: C901 -- a linear operator script
    args = parse_args(argv)

    try:
        api = ControlAPI(base_url=args.control_api_url)
        rows = fleet(api, status="running")
    except ControlAPIError as exc:
        return fail(str(exc))

    if not rows:
        print("no running tenants -- nothing to roll")
        return 0

    print(f"fleet     {len(rows)} running tenant(s)")
    print(f"target    {args.image_tag}")
    if args.dry_run:
        print("DRY RUN -- no redeploys will be issued")

    failures: list[str] = []
    aborted = False

    # --- 1. canary ----------------------------------------------------------
    # A rollback deliberately skips the canary gate: the fleet is already on a bad
    # image, and stopping to prove one tenant recovers first just extends the outage.
    if args.canary and not args.rollback:
        if args.canary not in rows:
            return fail(f"canary {args.canary} is not a running tenant")
        print(f"\ncanary    {args.canary}")
        if already_on(rows[args.canary], args.image_tag):
            print(f"  {args.canary}: already on the target image, skipping")
        elif not roll_one(api, args.canary, args):
            print("\nCANARY FAILED -- the rest of the fleet is untouched.")
            print("Investigate, then roll this tenant back:")
            print(f"  upgrade_drill.py --canary {args.canary} --image-tag <previous>")
            return 2
        if not args.roll:
            print("\ncanary converged. Re-run with --roll to continue.")
            return 0

    # --- 2. the rest --------------------------------------------------------
    if args.roll or args.rollback:
        remaining = list(rows) if args.rollback else [t for t in rows if t != args.canary]
        print(f"\nrolling   {len(remaining)} tenant(s)")
        for index, tenant_id in enumerate(remaining):
            if already_on(rows.get(tenant_id), args.image_tag):
                print(f"  {tenant_id}: already on the target image, skipping")
                continue

            if roll_one(api, tenant_id, args):
                continue

            failures.append(tenant_id)
            # A rollback never aborts: pushing the whole fleet back IS the goal, and
            # a tenant that will not come back needs a human either way.
            if not args.rollback and len(failures) >= args.max_failures:
                print(
                    f"\nABORTED after {len(failures)} failure(s) "
                    f"(--max-failures {args.max_failures}). "
                    f"{len(remaining) - index - 1} tenant(s) untouched."
                )
                aborted = True
                break

    # --- 3. report ----------------------------------------------------------
    print()
    try:
        after = fleet(api, status="running")
        on_target = sum(1 for row in after.values() if already_on(row, args.image_tag))
        print(f"converged {on_target}/{len(after)} running tenant(s) on {args.image_tag}")
    except ControlAPIError as exc:  # reporting must not change the outcome
        print(f"(could not read the final fleet state: {exc})")

    if failures:
        print(f"FAILED    {', '.join(failures)}")
        return 2 if aborted else 1
    print("OK")
    return 0


def _detail(response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except Exception:  # noqa: BLE001
        return response.text[:300]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
