"""Railway public GraphQL API client.

Endpoint: https://backboard.railway.com/graphql/v2, `Authorization: Bearer <token>`
(account or workspace token). Docs: https://docs.railway.com/guides/public-api,
https://docs.railway.com/guides/manage-services, https://docs.railway.com/guides/api-cookbook

!! VERIFICATION STATUS !!
No Railway project existed when this was written, so none of these operations have
been executed against the live API. Every mutation therefore lives in its own small,
heavily-commented method, and `tests/test_railway_client.py` pins the exact request
payload we send. When the first real project exists, run one provision end-to-end;
any schema mismatch will surface as a `RailwayError` naming the offending field, and
the fix is confined to a single method + its test.

Confidence per operation:
  HIGH   -- serviceCreate, volumeCreate, variableCollectionUpsert,
            serviceInstanceDeploy, deployments  (verbatim from Railway's API cookbook)
  MEDIUM -- serviceInstanceUpdate (documented mutation; the `sleepApplication` and
            `source.image` fields are documented as serviceInstance settings but the
            exact input nesting is unverified), serviceDelete, project.services
            pagination shape
  LOW    -- deploymentStop (mutation name from Railway's schema explorer; signature
            `deploymentStop(id: String!): Boolean!` is unverified)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from control_api.config import get_settings

log = logging.getLogger(__name__)


class RailwayError(RuntimeError):
    """Any non-success response from Railway (HTTP or GraphQL `errors`)."""


# Substrings in a Railway error message that mean "the thing you asked for is
# already in the state you wanted". Treated as success so every step is re-runnable.
_ALREADY_EXISTS_HINTS = ("already exists", "already has", "duplicate")
_NOT_FOUND_HINTS = ("not found", "does not exist", "no such")


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
        """Destroy the service and its deployments. Idempotent: an already-deleted
        service reports success-as-False rather than raising, because deletion is
        the crypto-shred path and must be safe to re-run."""
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

        Returns None both for "no volume" and for "could not tell" -- callers treat
        it as advisory. It exists so retries do not depend solely on pattern-matching
        Railway's duplicate-volume error text (`_ALREADY_EXISTS_HINTS`); if Railway
        words that error differently, a retry would otherwise burn every attempt and
        hard-fail an otherwise healthy tenant.
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

        Returns the volume id, or None when Railway says one already exists (a retry
        after a partial failure). Callers should run `find_volume_for_service` first;
        the error-substring path below is the fallback for when that probe cannot
        answer.
        """
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
        try:
            data = self._gql(mutation, {"input": payload})
        except RailwayError as exc:
            if _matches(str(exc), _ALREADY_EXISTS_HINTS):
                log.info("volume already attached to %s at %s", service_id, mount_path)
                return None
            raise
        return (data.get("volumeCreate") or {}).get("id")

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
        """Trigger a deployment of the service in our environment."""
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
        False when there is nothing running (already stopped -> idempotent).
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
            if _matches(str(exc), _NOT_FOUND_HINTS):
                return False
            raise
        return True
