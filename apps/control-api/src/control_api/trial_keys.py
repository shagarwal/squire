"""Trial LLM keys, issued by the LiteLLM trial proxy (implementation-plan Task 0.5).

The trial runs on *our* Anthropic org key fronted by LiteLLM, with a per-tenant
virtual key carrying a hard $2 budget (PRD §5.3). Two lifecycle events:

  * created at provision time   -> tenant boots with ANTHROPIC_API_KEY/BASE_URL
  * deleted on LLM-connect or trial expiry -> "their LLM traffic never touches our
    infrastructure again" (PRD §2)

Privacy note: we delete keys **by alias**, never by key material, precisely so the
control DB never needs a column that could hold an API key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from control_api.config import get_settings

log = logging.getLogger(__name__)


class TrialKeyError(RuntimeError):
    pass


def trial_key_alias(tenant_id: str) -> str:
    """Deterministic alias so revocation needs nothing but the tenant id."""
    return f"squire-trial-{tenant_id}"


@dataclass
class TrialKey:
    key: str
    alias: str


class TrialKeyClient:
    """LiteLLM admin API client (`/key/generate`, `/key/delete`).

    If `LITELLM_BASE_URL` is unset the client is *disabled* and every method is a
    no-op returning None/False. That is deliberate: Task 0.5 (trial proxy) lands
    after Task 0.3, and provisioning must not be blocked on it.
    """

    def __init__(
        self,
        base_url: str | None = None,
        master_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.litellm_base_url).rstrip("/")
        self.master_key = master_key if master_key is not None else settings.litellm_master_key
        self._client = client
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _post(self, path: str, payload: dict) -> httpx.Response:
        client = self._client or httpx.Client(timeout=self._settings.litellm_timeout_seconds)
        try:
            return client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self.master_key}"},
            )
        except httpx.HTTPError as exc:
            raise TrialKeyError(f"LiteLLM {path} failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

    def create_trial_key(self, tenant_id: str) -> TrialKey | None:
        """Mint a budgeted virtual key for one tenant's 3-day trial."""
        if not self.enabled:
            log.warning("LiteLLM not configured; tenant %s gets no trial key", tenant_id)
            return None

        alias = trial_key_alias(tenant_id)
        payload = {
            # The load-bearing control: LiteLLM hard-stops the key at this spend.
            "max_budget": self._settings.trial_max_budget_usd,
            # Key self-expires with the trial window, so an orphaned tenant cannot
            # keep spending if our expiry job ever misses it.
            "duration": self._settings.trial_duration,
            "key_alias": alias,
            "metadata": {"tenant_id": tenant_id, "source": "squire-trial"},
            # --- Best-effort secondary limits ---------------------------------
            # `budget_duration` resets `max_budget` on a rolling window; combined
            # with a smaller daily figure it approximates the PRD's "75 msgs/day".
            # LiteLLM's exact semantics for combining these with the hard cap vary
            # by version, so these are advisory: the $2 `max_budget` above is the
            # guarantee, and the per-day message count is additionally enforced by
            # the tenant/concierge. Verify against the deployed LiteLLM in Task 0.5.
            "budget_duration": "1d",
            "soft_budget": self._settings.trial_daily_budget_usd,
            "rpm_limit": self._settings.trial_rpm_limit,
        }
        response = self._post("/key/generate", payload)
        if response.status_code >= 400:
            raise TrialKeyError(
                f"LiteLLM /key/generate HTTP {response.status_code}: {response.text[:300]}"
            )
        key = (response.json() or {}).get("key")
        if not key:
            raise TrialKeyError("LiteLLM /key/generate returned no key")
        return TrialKey(key=key, alias=alias)

    def delete_trial_key(self, alias: str) -> bool:
        """Revoke by alias. Returns False (not an error) if there was nothing to
        revoke -- LLM-connect and trial-expiry can race and both call this."""
        if not self.enabled or not alias:
            return False
        # NOTE: `key_aliases` is supported by current LiteLLM; older builds accept
        # only `keys`. We deliberately cannot fall back to `keys` -- we never stored
        # the key material. If a deployment rejects aliases, upgrade LiteLLM.
        response = self._post("/key/delete", {"key_aliases": [alias]})
        if response.status_code >= 400:
            log.warning(
                "LiteLLM /key/delete for %s returned %s: %s",
                alias,
                response.status_code,
                response.text[:200],
            )
            return False
        return True
