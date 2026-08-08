"""Railway GraphQL client tests -- all HTTP mocked.

NOTE: Railway's public GraphQL schema could not be exercised live while writing
this (no Railway project exists yet). These tests pin the *shape* of each request
we send -- operation name, variable payload -- so that when the schema is verified
against the real API, any mismatch shows up as a single failing assertion here
rather than a mystery at provision time.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from control_api.clients.railway import RailwayClient, RailwayError

GQL = "https://backboard.railway.com/graphql/v2"


def gql_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


@pytest.fixture
def railway() -> RailwayClient:
    return RailwayClient()


@respx.mock
def test_auth_header_is_bearer_token(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"me": {"id": "u1"}}))
    railway._gql("query { me { id } }", {})
    assert route.calls.last.request.headers["authorization"] == "Bearer railway-test-token"
    assert route.calls.last.request.headers["content-type"].startswith("application/json")


@respx.mock
def test_graphql_errors_raise(railway):
    respx.post(GQL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Not Authorized"}]})
    )
    with pytest.raises(RailwayError, match="Not Authorized"):
        railway._gql("query { me { id } }", {})


@respx.mock
def test_http_errors_raise(railway):
    respx.post(GQL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(RailwayError):
        railway._gql("query { me { id } }", {})


@respx.mock
def test_create_service_from_image(railway):
    route = respx.post(GQL).mock(
        return_value=gql_response({"serviceCreate": {"id": "svc-1", "name": "tenant-abc"}})
    )
    service_id = railway.create_service(
        name="tenant-abc",
        image="ghcr.io/shagarwal/squire/hermes-tenant:v0",
        variables={"TENANT_ID": "abc"},
    )
    assert service_id == "svc-1"

    body = route.calls.last.request.read().decode()
    assert "serviceCreate" in body
    payload = route.calls.last.request
    import json

    sent = json.loads(payload.read())
    inp = sent["variables"]["input"]
    assert inp["projectId"] == "proj-123"
    assert inp["environmentId"] == "env-123"
    assert inp["name"] == "tenant-abc"
    assert inp["source"] == {"image": "ghcr.io/shagarwal/squire/hermes-tenant:v0"}
    assert inp["variables"] == {"TENANT_ID": "abc"}


@respx.mock
def test_find_service_by_name(railway):
    respx.post(GQL).mock(
        return_value=gql_response(
            {
                "project": {
                    "services": {
                        "edges": [
                            {"node": {"id": "svc-1", "name": "tenant-abc"}},
                            {"node": {"id": "svc-2", "name": "tenant-def"}},
                        ]
                    }
                }
            }
        )
    )
    assert railway.find_service_by_name("tenant-def") == "svc-2"
    assert railway.find_service_by_name("tenant-nope") is None


@respx.mock
def test_attach_volume(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"volumeCreate": {"id": "vol-1"}}))
    volume_id = railway.attach_volume("svc-1", "/opt/data")
    assert volume_id == "vol-1"

    import json

    inp = json.loads(route.calls.last.request.read())["variables"]["input"]
    assert inp == {
        "projectId": "proj-123",
        "environmentId": "env-123",
        "serviceId": "svc-1",
        "mountPath": "/opt/data",
    }


@respx.mock
def test_attach_volume_is_idempotent_when_railway_says_it_exists(railway):
    """A retry after a partial failure must not explode -- Railway rejects a second
    volume on the same mount path, and we treat that as success."""
    respx.post(GQL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "A volume already exists for this service"}]},
        )
    )
    assert railway.attach_volume("svc-1", "/opt/data") is None


@respx.mock
def test_set_variables(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"variableCollectionUpsert": True}))
    railway.set_variables("svc-1", {"A": "1", "B": "2"})

    import json

    sent = json.loads(route.calls.last.request.read())
    assert "variableCollectionUpsert" in sent["query"]
    inp = sent["variables"]["input"]
    assert inp["projectId"] == "proj-123"
    assert inp["environmentId"] == "env-123"
    assert inp["serviceId"] == "svc-1"
    assert inp["variables"] == {"A": "1", "B": "2"}
    # We batch variables and deploy explicitly afterwards, so the upsert must not
    # trigger its own deploy.
    assert inp["skipDeploys"] is True


@respx.mock
def test_configure_service_instance_sets_sleep(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"serviceInstanceUpdate": True}))
    railway.configure_service_instance("svc-1", sleep_application=True, region=None)

    import json

    sent = json.loads(route.calls.last.request.read())
    assert "serviceInstanceUpdate" in sent["query"]
    assert sent["variables"]["serviceId"] == "svc-1"
    assert sent["variables"]["environmentId"] == "env-123"
    assert sent["variables"]["input"]["sleepApplication"] is True


@respx.mock
def test_deploy(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"serviceInstanceDeploy": True}))
    railway.deploy("svc-1")

    import json

    sent = json.loads(route.calls.last.request.read())
    assert "serviceInstanceDeploy" in sent["query"]
    assert sent["variables"] == {"serviceId": "svc-1", "environmentId": "env-123"}


@respx.mock
def test_stop_service_stops_latest_deployment(railway):
    """Railway has no 'stop service'; you stop its current deployment."""
    responses = [
        gql_response(
            {
                "deployments": {
                    "edges": [{"node": {"id": "dep-9", "status": "SUCCESS", "createdAt": "x"}}]
                }
            }
        ),
        gql_response({"deploymentStop": True}),
    ]
    respx.post(GQL).mock(side_effect=responses)
    assert railway.stop_service("svc-1") is True


@respx.mock
def test_stop_service_no_deployment_is_noop(railway):
    respx.post(GQL).mock(return_value=gql_response({"deployments": {"edges": []}}))
    assert railway.stop_service("svc-1") is False


@respx.mock
def test_delete_service(railway):
    route = respx.post(GQL).mock(return_value=gql_response({"serviceDelete": True}))
    assert railway.delete_service("svc-1") is True

    import json

    sent = json.loads(route.calls.last.request.read())
    assert "serviceDelete" in sent["query"]
    assert sent["variables"]["id"] == "svc-1"


@respx.mock
def test_delete_service_tolerates_missing_service(railway):
    """Deleting an already-deleted service must be idempotent (crypto-shred reruns)."""
    respx.post(GQL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Service not found"}]})
    )
    assert railway.delete_service("svc-1") is False
