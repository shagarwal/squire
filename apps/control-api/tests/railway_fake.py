"""A respx handler that fakes the Railway GraphQL endpoint.

Railway multiplexes everything over one POST URL, so an ordered `side_effect` list
would be extremely brittle. Instead we dispatch on the operation name embedded in
the query string, which also lets tests assert "which mutations were called, with
what" after the fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

GQL_URL = "https://backboard.railway.com/graphql/v2"


@dataclass
class FakeRailway:
    """Stateful fake: records calls and lets tests force per-operation failures."""

    existing_services: dict[str, str] = field(default_factory=dict)  # name -> id
    calls: list[tuple[str, dict]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)
    latest_deployment_id: str | None = "dep-1"
    #: Service ids that already have a volume attached (pre-flight probe answers).
    existing_volume_service_ids: set[str] = field(default_factory=set)
    #: Volume ids passed to volumeDelete, in order.
    deleted_volume_ids: list[str] = field(default_factory=list)
    #: Service ids passed to serviceDelete, in order.
    deleted_service_ids: list[str] = field(default_factory=list)
    #: How many probes to answer with None before reporting the volume. Models the
    #: real API's post-create list lag.
    volume_probe_lag: int = 0
    _volume_probes: int = 0
    #: Variable NAMES the read-back probe reports. None = "every name we were sent",
    #: which is the healthy case; set it explicitly to simulate a lost variable.
    variable_names: set[str] | None = None
    _upserted_names: set[str] = field(default_factory=set)

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        query = payload["query"]
        variables = payload.get("variables", {})
        op = self._operation(query)
        self.calls.append((op, variables))

        if op in self.fail_on:
            return httpx.Response(200, json={"errors": [{"message": f"forced failure: {op}"}]})

        if op == "project":
            # One operation name, two queries -- disambiguate on the selection set.
            if "volumes" in query:  # find_volume_for_service
                self._volume_probes += 1
                # The real API's volume list lags behind volumeCreate; `volume_probe_lag`
                # makes the first N probes report nothing even though a volume exists.
                if self._volume_probes <= self.volume_probe_lag:
                    return self._ok({"project": {"volumes": {"edges": []}}})
                edges = [
                    {
                        "node": {
                            "id": "vol-existing",
                            "volumeInstances": {
                                "edges": [{"node": {"id": "vi-1", "serviceId": sid}}]
                            },
                        }
                    }
                    for sid in self.existing_volume_service_ids
                ]
                return self._ok({"project": {"volumes": {"edges": edges}}})
            # find_service_by_name
            edges = [
                {"node": {"id": sid, "name": name}}
                for name, sid in self.existing_services.items()
            ]
            return self._ok({"project": {"services": {"edges": edges}}})

        if op == "variables":  # get_variable_names -- names only, values are dummies
            names = (
                self.variable_names
                if self.variable_names is not None
                else self._upserted_names
            )
            return self._ok({"variables": {name: "redacted" for name in names}})

        if op == "serviceCreate":
            name = variables["input"]["name"]
            sid = f"svc-{len(self.existing_services) + 1}"
            self.existing_services[name] = sid
            return self._ok({"serviceCreate": {"id": sid, "name": name}})

        if op == "volumeCreate":
            # Mirrors the live behaviour that makes the probe mandatory: Railway
            # does NOT reject a duplicate, it just makes another volume.
            self.existing_volume_service_ids.add(variables["input"]["serviceId"])
            return self._ok({"volumeCreate": {"id": "vol-1"}})

        if op == "volumeDelete":
            self.deleted_volume_ids.append(variables["volumeId"])
            return self._ok({"volumeDelete": True})

        if op == "variableCollectionUpsert":
            self._upserted_names.update(variables["input"]["variables"].keys())
            return self._ok({"variableCollectionUpsert": True})

        if op == "serviceInstanceUpdate":
            return self._ok({"serviceInstanceUpdate": True})

        if op == "serviceInstanceDeploy":
            return self._ok({"serviceInstanceDeploy": True})

        if op == "deployments":
            edges = (
                [{"node": {"id": self.latest_deployment_id, "status": "SUCCESS"}}]
                if self.latest_deployment_id
                else []
            )
            return self._ok({"deployments": {"edges": edges}})

        if op == "deploymentStop":
            return self._ok({"deploymentStop": True})

        if op == "serviceDelete":
            self.deleted_service_ids.append(variables["id"])
            # Live behaviour: serviceDelete does NOT remove the volume. The fake
            # keeps `existing_volume_service_ids` untouched on purpose, so a test
            # that deletes in the wrong order can still observe the orphan.
            return self._ok({"serviceDelete": True})

        raise AssertionError(f"unexpected Railway operation: {op}\n{query}")

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _ok(data: dict) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    @staticmethod
    def _operation(query: str) -> str:
        """Extract the operation name (`mutation serviceCreate(...)` -> serviceCreate)."""
        head = query.strip().split("{", 1)[0].strip()
        head = head.removeprefix("mutation").removeprefix("query").strip()
        return head.split("(")[0].strip()

    def variables_for(self, op: str) -> dict:
        """Last variables sent for a given operation."""
        for name, variables in reversed(self.calls):
            if name == op:
                return variables
        raise AssertionError(f"{op} was never called; saw {[c[0] for c in self.calls]}")

    def operations(self) -> list[str]:
        return [c[0] for c in self.calls]
