"""Constants control-api shares with the tenant image, pinned against the source.

Every value here is agreed between two files owned by two different tasks, edited
at different times, with nothing but a comment connecting them. That is exactly the
shape of a bug that survives review: both sides look correct in isolation.

This suite reads `tenant-image/Dockerfile` directly rather than restating its values
-- a duplicated constant is the drift it is meant to catch. Same approach as
`apps/trial-proxy/tests/test_config.py`, which pins the model-name contract.

REGRESSION THIS EXISTS FOR: control-api defaulted the volume mount to
`/root/.hermes` while the tenant image's volume, HOME and HERMES_HOME were all
`/opt/data`. Nothing degrades gracefully in that situation -- the entrypoint's
durability gate refuses to boot when its volume path is not a real mount and
TENANT_ID is set (pg0 would otherwise write the tenant's memory database to
ephemeral container storage). Every live provision would have produced a tenant
that would not start.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[3] / "tenant-image" / "Dockerfile"


@pytest.fixture(scope="module")
def dockerfile_env() -> dict[str, str]:
    """Every `ENV KEY=value` assignment in the tenant Dockerfile, flattened.

    Handles the multi-line `ENV A=1 \\\n    B=2` form the Dockerfile uses
    throughout. Values are unquoted; that is all these particular constants need.
    """
    if not DOCKERFILE.is_file():  # pragma: no cover - not in this checkout
        pytest.skip(f"tenant image not present at {DOCKERFILE}")

    text = DOCKERFILE.read_text(encoding="utf-8")
    # Join backslash continuations so a multi-line ENV block reads as one line.
    text = re.sub(r"\\\s*\n\s*", " ", text)

    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("ENV "):
            continue
        for key, value in re.findall(r'([A-Z0-9_]+)=("[^"]*"|\S+)', line[4:]):
            env[key] = value.strip('"')
    return env


def test_the_dockerfile_declares_the_paths_we_depend_on(dockerfile_env):
    """Guard the guard: if the ENV parsing above silently found nothing, every
    other assertion in this file would pass vacuously."""
    for key in ("HOME", "HERMES_HOME", "SQUIRE_VOLUME", "PORT"):
        assert key in dockerfile_env, f"tenant-image/Dockerfile no longer sets {key}"


def test_volume_mount_path_matches_the_tenant_images_volume(settings, dockerfile_env):
    """The blocker this file was written for.

    control-api tells Railway where to mount the volume; the tenant image decides
    where it expects to find it. If they disagree the tenant does not boot at all.
    """
    assert settings.tenant_volume_mount_path == dockerfile_env["SQUIRE_VOLUME"], (
        f"control-api mounts the tenant volume at {settings.tenant_volume_mount_path!r} "
        f"but the tenant image expects it at {dockerfile_env['SQUIRE_VOLUME']!r}. "
        "The entrypoint's durability gate would refuse to boot every tenant. "
        "Change both, or neither."
    )


def test_home_and_hermes_home_are_the_volume(dockerfile_env):
    """Inside the image, all three names must be the same path.

    HOME is what pg0 derives its data root from, so a HOME that is not the mounted
    volume means the tenant's long-term memory is destroyed on every redeploy --
    silently, with no error anywhere. HERMES_HOME is what every Squire script
    resolves state against.
    """
    volume = dockerfile_env["SQUIRE_VOLUME"]
    assert dockerfile_env["HOME"] == volume
    assert dockerfile_env["HERMES_HOME"] == volume


def test_tenant_port_matches_the_images_port(settings, dockerfile_env):
    """ingress forwards to `internal_url`, which control-api builds from this port
    and the image serves the webhook shim on."""
    assert str(settings.tenant_port) == dockerfile_env["PORT"]


# ---------------------------------------------------------------------------
# ingress -> control-api: the typing-on-wake nudge
#
# Same drift-catching idea as the Dockerfile pins above, applied to the other
# service in this repo: ingress fire-and-forgets POST /internal/wake-typing
# {bot_id, chat_id} when it buffers an update for a sleeping tenant. The route
# path and the two payload keys are agreed between apps/ingress/src/ingress/
# wake_nudge.py and control_api.routers.internal with nothing but this test
# connecting them -- and because the nudge is deliberately fire-and-forget
# (ingress swallows every failure), a drifted path would 404 SILENTLY forever:
# no error anywhere, just users staring at silent chats again.
# ---------------------------------------------------------------------------

INGRESS_WAKE_NUDGE = (
    Path(__file__).resolve().parents[3] / "apps" / "ingress" / "src" / "ingress" / "wake_nudge.py"
)


@pytest.fixture(scope="module")
def wake_nudge_source() -> str:
    if not INGRESS_WAKE_NUDGE.is_file():  # pragma: no cover - not in this checkout
        pytest.skip(f"ingress not present at {INGRESS_WAKE_NUDGE}")
    return INGRESS_WAKE_NUDGE.read_text(encoding="utf-8")


def test_ingress_nudges_the_route_control_api_serves(client, wake_nudge_source):
    assert "/internal/wake-typing" in wake_nudge_source, (
        "ingress's wake nudge no longer targets /internal/wake-typing; "
        "if the path moved, move control-api's route with it (and vice versa)."
    )
    # Probe the route over HTTP rather than introspecting app.routes (FastAPI
    # nests included routers, so route objects are awkward to enumerate): an
    # unauthenticated POST must hit the internal-token guard (401), which
    # simultaneously proves the route exists AND that it is behind auth. A
    # missing route would 404 here.
    assert client.post("/internal/wake-typing", json={}).status_code == 401, (
        "control-api no longer serves /internal/wake-typing (or serves it "
        "unauthenticated) but ingress still nudges it -- a removed route would "
        "404 silently forever (the nudge path swallows failures by design)."
    )


def test_ingress_nudge_payload_keys_match_the_request_schema(wake_nudge_source):
    from control_api.schemas import WakeTypingRequest

    # The exact json= literal ingress sends, pinned as source text.
    assert '{"bot_id": bot_id, "chat_id": chat_id}' in wake_nudge_source, (
        "ingress's nudge payload literal changed; update this pin and confirm "
        "the keys still match WakeTypingRequest."
    )
    # WakeTypingRequest forbids extras, so key drift on either side = 422s.
    assert set(WakeTypingRequest.model_fields) == {"bot_id", "chat_id"}


def test_tenant_image_is_pinned_to_an_explicit_tag():
    """`:latest` makes /fleet and the upgrade drill blind.

    Both work by comparing image references. If every tenant reports the same
    floating tag, "which tenants are on vN+1" has no answer, and a Railway redeploy
    can change what a tenant runs with no record of when or to what.

    Asserts the CLASS DEFAULT rather than the `settings` fixture: conftest sets
    TENANT_IMAGE for the test run, so reading the effective value would test the
    fixture and let the shipped default quietly rot back to `:latest`.
    """
    from control_api.config import Settings

    reference = Settings.model_fields["tenant_image"].default
    assert not reference.endswith(":latest"), "the TENANT_IMAGE default must not be :latest"
    last_segment = reference.rsplit("/", 1)[-1]
    assert ":" in last_segment or "@" in last_segment, (
        f"TENANT_IMAGE default ({reference!r}) carries no tag or digest -- Docker "
        "would resolve it as :latest"
    )


# ---------------------------------------------------------------------------
# tenant -> control-api: the llm-connected conversion call (1C). Same pattern
# as the wake-typing pins: route + payload keys agreed between
# tenant-image/bin/squire_connect.py and control_api with only this test
# connecting them, and the tenant caller swallows failures by design.
# ---------------------------------------------------------------------------

TENANT_CONNECT = (
    Path(__file__).resolve().parents[3] / "tenant-image" / "bin" / "squire_connect.py"
)


@pytest.fixture(scope="module")
def tenant_connect_source() -> str:
    if not TENANT_CONNECT.is_file():  # pragma: no cover - not in this checkout
        pytest.skip(f"tenant image not present at {TENANT_CONNECT}")
    return TENANT_CONNECT.read_text(encoding="utf-8")


def test_tenant_calls_the_route_control_api_serves(client, tenant_connect_source):
    assert "/internal/llm-connected" in tenant_connect_source, (
        "squire_connect no longer targets /internal/llm-connected; if the path "
        "moved, move control-api's route with it (and vice versa)."
    )
    # Unauthenticated probe: 401 proves the route exists AND is behind auth.
    assert client.post("/internal/llm-connected", json={}).status_code == 401


def test_tenant_payload_keys_match_the_request_schema(tenant_connect_source):
    from control_api.schemas import LlmConnectedRequest

    assert 'json.dumps({"tenant_id": tenant_id, "provider": provider})' in tenant_connect_source, (
        "squire_connect's notify payload literal changed; update this pin and "
        "confirm the keys still match LlmConnectedRequest."
    )
    assert set(LlmConnectedRequest.model_fields) == {"tenant_id", "provider"}
