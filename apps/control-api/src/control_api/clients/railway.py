"""Railway public GraphQL API client.

Endpoint: https://backboard.railway.com/graphql/v2, `Authorization: Bearer <token>`
(account or workspace token). Docs: https://docs.railway.com/guides/public-api,
https://docs.railway.com/guides/manage-services, https://docs.railway.com/guides/api-cookbook

!! VERIFICATION STATUS: VERIFIED LIVE 2026-08-08 !!
Every operation below was executed against the real API (staging project, scratch
services) and accepted exactly as written -- including `deploymentStop`, which had
been a guess, and `serviceInstanceUpdate`'s `source.image` nesting, which was proven
to take effect via read-back rather than being silently ignored.

Verified live: serviceCreate, serviceDelete, volumeCreate, volumeDelete,
variableCollectionUpsert, variables, serviceInstanceUpdate (incl. source.image and
sleepApplication), serviceInstanceDeploy, deployments, deploymentStop,
project.services (incl. `first:` pagination).

Behaviours the live run established, each of which the code below depends on:

  * **serviceDelete does NOT cascade to volumes.** The volume survives as a
    permanently billed orphan -- `isPendingDeletion` stays false and it was still
    there after 5.5 minutes of polling, so this is not delayed GC. Deprovisioning
    must call `delete_volume` FIRST, then `delete_service`; that exact ordering was
    verified to leave nothing behind. Service-first leaves the orphan.
  * **volumeCreate is not idempotent.** A second call with the same service +
    mountPath silently creates a DUPLICATE volume rather than erroring, so
    `attach_volume` must probe first -- the "already exists" error branch this code
    used to rely on is unreachable.
  * **Deleting an already-deleted service SUCCEEDS** (no error), so `delete_service`
    is naturally idempotent.
  * **Stopping an already-stopped deployment** returns "Deployment is not
    stoppable", which is a normal idempotent outcome, not a failure.
  * **serviceCreate auto-triggers an initial deployment.** See `deploy()`.
  * **volumeCreate against a DELETED service id resurrects the service.** Never
    attach a volume without confirming the service is live.
  * **No rate-limit headers are exposed** (40 requests in 5.6s went unthrottled and
    no `X-RateLimit-*`/`Retry-After` appeared). The service-creation rate limit
    question in implementation-plan.md §7 risk 2 therefore has no client-visible
    answer; our own bounded retries + backoff remain the only throttle.
  * Cloudflare 403s the default Python-urllib User-Agent; httpx is unaffected.
    Anything scripted against this API must not use urllib.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from control_api.config import get_settings

log = logging.getLogger(__name__)


class RailwayError(RuntimeError):
    """Any non-success response from Railway (HTTP or GraphQL `errors`)."""


# Substrings in a Railway error message that mean "the thing you asked for is
# already in the state you wanted". Treated as success so every step is re-runnable.
#
# NOTE: there is deliberately no "already exists" set for volumes. Live testing
# proved volumeCreate never reports a duplicate -- it just creates a second volume --
# so `attach_volume` guards with a probe instead of an error branch.
_NOT_FOUND_HINTS = ("not found", "does not exist", "no such")
# Railway's wording when a deployment cannot be stopped because it is not running.
# Verified live against an already-stopped tenant: "Deployment is not stoppable".
_NOT_STOPPABLE_HINTS = ("not stoppable", "not running")


def _matches(message: str, hints: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in hints)


class RailwayClient:
    """Thin, synchronous, one-method-per-mutation wrapper."""

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        project_id: str | None = None,
        environment_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.token = token if token is not None else settings.railway_api_token
        self.url = url or settings.railway_graphql_url
        self.project_id = project_id if project_id is not None else settings.railway_project_id
        self.environment_id = (
            environment_id if environment_id is not None else settings.railway_environment_id
        )
        self._client = client
        self._timeout = settings.railway_timeout_seconds
        self._volume_probe_delay = settings.railway_volume_probe_delay_seconds

    # -- transport ---------------------------------------------------------

    def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST one GraphQL document. Raises `RailwayError` on any failure.

        GraphQL is 200-on-error, so the `errors` key must be checked explicitly --
        forgetting that is the classic way to silently half-provision a tenant.
        """
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:  # network/timeout
            raise RailwayError(f"Railway request failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code >= 400:
            raise RailwayError(
                f"Railway HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RailwayError(f"Railway returned non-JSON: {response.text[:200]}") from exc

        if body.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in body["errors"])
            raise RailwayError(messages)
        return body.get("data") or {}

    # -- services ----------------------------------------------------------

    def find_service_by_name(self, name: str) -> str | None:
        """Idempotency probe: has this tenant's service already been created?

        Used before `serviceCreate` so a crash between "Railway created it" and
        "we committed the id" doesn't leave an orphan and then bill for a twin.

        Project-scoped with no `environmentId`, which is correct rather than an
        oversight: in Railway's model a SERVICE belongs to a project and has one
        instance per environment, so a service id is not environment-specific and
        there is nothing to disambiguate. Names are unique per project and ours
        embed the tenant id (`tenant-<id>`), so a match is unambiguous. The
        environment only enters when we configure or deploy an instance, and both
        of those paths do pass `environmentId`.
        """
        # `first: 500` because a 1,000-tenant fleet is 1,000+ services and the
        # default page size would silently hide ours, causing a duplicate create.
        # TODO(Phase 1): follow `pageInfo.hasNextPage` properly once the fleet can
        # exceed 500 services -- see implementation-plan.md §7 risk 2.
        query = """
        query project($id: String!) {
          project(id: $id) {
            services(first: 500) { edges { node { id name } } }
          }
        }
        """
        data = self._gql(query, {"id": self.project_id})
        edges = ((data.get("project") or {}).get("services") or {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            if node.get("name") == name:
                return node.get("id")
        return None

    def get_variable_names(self, service_id: str) -> set[str] | None:
        """Names of the variables currently set on a service. **Never values.**

        Used to confirm an upsert actually landed. Deliberately returns only keys:
        reading tenant variable *values* back into the control plane would hand it
        the DEK it is architecturally supposed to never hold (PRD §4).
        """
        query = """
        query variables($projectId: String!, $environmentId: String!, $serviceId: String) {
          variables(
            projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId
          )
        }
        """
        data = self._gql(
            query,
            {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": service_id,
            },
        )
        variables = data.get("variables")
        if not isinstance(variables, dict):
            return None
        return set(variables.keys())

    # -- public domains (1C connect page) ----------------------------------
    # !! UNVERIFIED against the live API (unlike everything above -- see the
    # module header). Shapes follow Railway's public schema:
    # serviceDomainCreate(input:{environmentId, serviceId}) and the `domains`
    # query. Verify live during the 1C staging rollout and update this note.

    def get_service_domain(self, service_id: str) -> str | None:
        """The service's generated public domain, or None if none exists yet.

        The probe half of create_service_domain's idempotency: domain creation
        has not been proven idempotent, so (exactly like attach_volume) we
        never mutate without first establishing absence.
        """
        query = """
        query domains($projectId: String!, $environmentId: String!, $serviceId: String!) {
          domains(
            projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId
          ) {
            serviceDomains { domain }
          }
        }
        """
        data = self._gql(
            query,
            {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": service_id,
            },
        )
        for entry in ((data.get("domains") or {}).get("serviceDomains")) or []:
            domain = (entry or {}).get("domain")
            if domain:
                return domain
        return None

    def create_service_domain(self, service_id: str) -> str:
        """Generate a Railway public domain (`tenant-….up.railway.app`) for the
        service. Callers must probe with get_service_domain first."""
        mutation = """
        mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) { id domain }
        }
        """
        data = self._gql(
            mutation,
            {"input": {"environmentId": self.environment_id, "serviceId": service_id}},
        )
        domain = (data.get("serviceDomainCreate") or {}).get("domain")
        if not domain:
            raise RailwayError(f"serviceDomainCreate returned no domain: {data}")
        return domain

    def create_service(
        self,
        name: str,
        image: str,
        variables: dict[str, str] | None = None,
    ) -> str:
        """Create a tenant service from a GHCR image.

        `ServiceCreateInput` accepts `source: { image: ... }` for image-backed
        services (vs `{ repo: ... }` for GitHub). Variables can be seeded here, but
        we set them in a dedicated step so that step stays independently retryable.
        """
        mutation = """
        mutation serviceCreate($input: ServiceCreateInput!) {
          serviceCreate(input: $input) { id name }
        }
        """
        payload: dict[str, Any] = {
            "projectId": self.project_id,
            "environmentId": self.environment_id,
            "name": name,
            "source": {"image": image},
        }
        if variables:
            payload["variables"] = variables
        data = self._gql(mutation, {"input": payload})
        service_id = (data.get("serviceCreate") or {}).get("id")
        if not service_id:
            raise RailwayError(f"serviceCreate returned no id: {data}")
        return service_id

    def delete_service(self, service_id: str) -> bool:
        """Destroy the service and its deployments.

        **This does NOT delete the service's volume.** Verified live: the volume
        survives as a permanently billed orphan, detached from any service, with
        `isPendingDeletion` false even 5.5 minutes later. Callers doing a
        crypto-shred MUST call `delete_volume` FIRST -- see `delete_volume` and
        `provisioning.delete_tenant`.

        Idempotent because Railway says so: deleting an already-deleted service
        returns success rather than an error (verified live). The not-found branch
        below is therefore defensive only, kept in case that behaviour changes.

        DELIBERATELY NOT ENVIRONMENT-SCOPED, and this is the one place where that
        needs saying out loud. `serviceDelete` accepts an optional `environmentId`,
        and passing it would be the safer-looking choice -- but it changes the
        meaning of the call: it deletes that environment's service INSTANCE rather
        than the service itself, leaving the service behind. We want the service
        gone, so the unscoped form is the correct one here.

        The cross-environment risk that scoping would guard against does not exist
        in this architecture: staging and prod are separate Railway PROJECTS
        (`railway_project_id`), not two environments of one project, and this
        client is bound to a single project. There is no path from a prod call to
        a staging service.
        """
        mutation = """
        mutation serviceDelete($id: String!) {
          serviceDelete(id: $id)
        }
        """
        try:
            self._gql(mutation, {"id": service_id})
        except RailwayError as exc:
            if _matches(str(exc), _NOT_FOUND_HINTS):
                log.info("service %s already gone", service_id)
                return False
            raise
        return True

    # -- volumes -----------------------------------------------------------

    def find_volume_for_service(self, service_id: str) -> str | None:
        """Pre-flight idempotency probe: does this service already have a volume?

        Returns the volume id, or None for both "no volume" and "could not tell".

        This is the ONLY protection against duplicate volumes -- `volumeCreate` was
        verified live to create a second volume rather than reject the request, so
        there is no error branch to fall back on. `attach_volume` therefore treats a
        positive answer as binding, and re-probes once before accepting a None (the
        list lags a few seconds behind a create).

        Two caveats worth knowing when reading this:
          * a probe failure is indistinguishable from "no volume", which is why
            `attach_volume` retries rather than trusting a single None;
          * the mountPath is deliberately not matched on -- one volume per tenant
            service is the invariant, and a volume at an unexpected path is still a
            volume we must not duplicate (and must delete at crypto-shred time).

        No `environmentId` for the same reason as `find_service_by_name`, plus a
        stronger one: the match here is on `serviceId`, which is already unique
        within the project. Filtering by environment could only ever narrow a set
        that has at most one member.
        """
        query = """
        query project($id: String!) {
          project(id: $id) {
            volumes {
              edges {
                node {
                  id
                  volumeInstances {
                    edges { node { id serviceId mountPath } }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = self._gql(query, {"id": self.project_id})
        except RailwayError:
            # Query shape is unverified against live Railway; never let the probe
            # itself break provisioning.
            log.warning("volume pre-flight probe failed", exc_info=True)
            return None

        edges = ((data.get("project") or {}).get("volumes") or {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            instances = (node.get("volumeInstances") or {}).get("edges") or []
            for instance in instances:
                if (instance.get("node") or {}).get("serviceId") == service_id:
                    return node.get("id")
        return None

    def attach_volume(self, service_id: str, mount_path: str) -> str | None:
        """Create + attach the tenant's persistent volume (its `~/.hermes`).

        Returns the volume id (existing or newly created).

        **The probe is mandatory, not advisory.** Live testing proved `volumeCreate`
        is not idempotent: calling it twice for the same service + mountPath silently
        creates a SECOND volume rather than erroring. A duplicate is billed forever
        and the tenant only ever mounts one of them, so we must never issue the
        mutation without first establishing that no volume exists.

        The probe also lags: immediately after a create it still reports nothing, and
        only becomes correct a few seconds later. That matters on a retry after a
        crash between "Railway created the volume" and "we committed its id" -- so a
        single None is not trusted, and we re-probe after a short delay before
        concluding the service really has no volume. That costs a few seconds once
        per provision, which is cheap next to a permanently duplicated volume; tune
        via `RAILWAY_VOLUME_PROBE_DELAY_SECONDS` (0 disables the second probe).
        """
        existing = self._probe_volume_with_retry(service_id)
        if existing is not None:
            log.info("volume %s already attached to %s", existing, service_id)
            return existing

        mutation = """
        mutation volumeCreate($input: VolumeCreateInput!) {
          volumeCreate(input: $input) { id }
        }
        """
        payload = {
            "projectId": self.project_id,
            "environmentId": self.environment_id,
            "serviceId": service_id,
            "mountPath": mount_path,
        }
        data = self._gql(mutation, {"input": payload})
        return (data.get("volumeCreate") or {}).get("id")

    def _probe_volume_with_retry(self, service_id: str) -> str | None:
        """`find_volume_for_service`, re-tried once to cover the post-create lag."""
        existing = self.find_volume_for_service(service_id)
        if existing is not None:
            return existing
        delay = self._volume_probe_delay
        if delay <= 0:
            return None
        time.sleep(delay)
        return self.find_volume_for_service(service_id)

    def delete_volume(self, volume_id: str) -> bool:
        """Destroy a volume. **Must be called BEFORE `delete_service`.**

        `serviceDelete` does not cascade to volumes -- verified live, the volume
        survives as a permanently billed orphan that no longer appears under any
        service. Deleting the volume while its service is still live was verified to
        remove it cleanly and immediately.

        (Railway's docs describe `volumeDelete` scheduling a ~48h-delayed deletion
        when the volume still has an attached service. The live run did not produce
        such a retention record with this ordering; we mirror the verified ordering
        exactly rather than reasoning from the docs.)

        Idempotent: a missing volume is reported as False rather than raised, since
        this is the crypto-shred path and must be safe to re-run.
        """
        mutation = """
        mutation volumeDelete($volumeId: String!) {
          volumeDelete(volumeId: $volumeId)
        }
        """
        try:
            self._gql(mutation, {"volumeId": volume_id})
        except RailwayError as exc:
            if _matches(str(exc), _NOT_FOUND_HINTS):
                log.info("volume %s already gone", volume_id)
                return False
            raise
        return True

    # -- variables ---------------------------------------------------------

    def set_variables(
        self,
        service_id: str,
        variables: dict[str, str],
        replace: bool = False,
        skip_deploys: bool = True,
    ) -> None:
        """Upsert service variables.

        `skipDeploys=True` because we trigger the deploy ourselves in the next step;
        letting Railway auto-deploy here would race the volume/instance config.
        `replace=False` keeps the operation additive, which is what makes re-running
        this step harmless.
        """
        mutation = """
        mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
          variableCollectionUpsert(input: $input)
        }
        """
        payload = {
            "projectId": self.project_id,
            "environmentId": self.environment_id,
            "serviceId": service_id,
            "variables": variables,
            "replace": replace,
            "skipDeploys": skip_deploys,
        }
        self._gql(mutation, {"input": payload})

    # -- service instance config ------------------------------------------

    def configure_service_instance(
        self,
        service_id: str,
        sleep_application: bool | None = None,
        region: str | None = None,
        num_replicas: int | None = None,
        image: str | None = None,
    ) -> None:
        """Per-environment service settings.

        The only one that matters in Phase 0 is `sleepApplication` -- Railway's
        scale-to-zero, which is half of the unit-economics bet (implementation-plan
        §2). Everything else is here so Task 0.6's upgrade drill has a hook.

        `image` re-points the service at a different container image and is that
        hook: it is how `POST /internal/tenants/{id}/redeploy` rolls a tenant onto
        vN+1 (and, unchanged, back onto vN). Confidence MEDIUM, same as the rest of
        this mutation: `ServiceInstanceUpdateInput.source` mirrors the
        `ServiceCreateInput.source` shape that `create_service` already uses and
        that Railway's cookbook documents, but the nesting under
        serviceInstanceUpdate is unverified against the live API. If it is wrong,
        the failure is loud (a `RailwayError` naming the field) and confined to this
        one argument -- the drill aborts before touching the rest of the fleet,
        which is exactly what a canary is for.
        """
        mutation = """
        mutation serviceInstanceUpdate(
          $serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!
        ) {
          serviceInstanceUpdate(
            serviceId: $serviceId, environmentId: $environmentId, input: $input
          )
        }
        """
        payload: dict[str, Any] = {}
        if sleep_application is not None:
            payload["sleepApplication"] = sleep_application
        if region is not None:
            payload["region"] = region
        if num_replicas is not None:
            payload["numReplicas"] = num_replicas
        if image is not None:
            payload["source"] = {"image": image}
        if not payload:
            return
        self._gql(
            mutation,
            {
                "serviceId": service_id,
                "environmentId": self.environment_id,
                "input": payload,
            },
        )

    # -- deployments -------------------------------------------------------

    def deploy(self, service_id: str) -> None:
        """Trigger a deployment of the service in our environment.

        On a freshly-created service this is the SECOND deployment: `serviceCreate`
        auto-triggers an initial one (verified live). That first deployment is
        useless to us -- it fires before the volume is attached and before any
        variables are set, so the tenant would boot with no `~/.hermes`, no bot
        token and no DEK. This explicit deploy is the authoritative one and
        supersedes it.

        We deliberately do NOT try to skip it: the cost is one wasted deploy cycle
        on first provision, whereas skipping would risk a tenant whose only
        deployment predates its configuration. On re-provision (retry) there is no
        auto-deploy and this call is the only one.
        """
        mutation = """
        mutation serviceInstanceDeploy($serviceId: String!, $environmentId: String!) {
          serviceInstanceDeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """
        self._gql(mutation, {"serviceId": service_id, "environmentId": self.environment_id})

    def latest_deployment(self, service_id: str) -> dict[str, Any] | None:
        """Most recent deployment for the service, or None."""
        query = """
        query deployments($input: DeploymentListInput!) {
          deployments(input: $input, first: 1) {
            edges { node { id status createdAt } }
          }
        }
        """
        data = self._gql(
            query,
            {
                "input": {
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "serviceId": service_id,
                }
            },
        )
        edges = (data.get("deployments") or {}).get("edges") or []
        return edges[0]["node"] if edges else None

    def stop_service(self, service_id: str) -> bool:
        """"Stop" a tenant.

        Railway has no service-level stop; you stop the running deployment. Returns
        False when there was nothing to stop (already stopped -> idempotent).

        A tenant that is already stopped still HAS a latest deployment, and Railway
        rejects stopping it with "Deployment is not stoppable". That is the normal
        idempotent outcome -- trial expiry and the sleep/stop paths both re-run --
        so it is reported as False, not raised. Verified live.
        """
        deployment = self.latest_deployment(service_id)
        if not deployment:
            return False
        mutation = """
        mutation deploymentStop($id: String!) {
          deploymentStop(id: $id)
        }
        """
        try:
            self._gql(mutation, {"id": deployment["id"]})
        except RailwayError as exc:
            if _matches(str(exc), _NOT_STOPPABLE_HINTS):
                log.info("deployment %s already stopped", deployment["id"])
                return False
            if _matches(str(exc), _NOT_FOUND_HINTS):
                return False
            raise
        return True
