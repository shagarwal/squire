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
