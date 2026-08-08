"""Structural privacy guard (PRD §4).

> "**Structurally cannot hold** conversation content or plaintext credentials --
>  the schema has no columns for them."

This test turns that sentence into a build-breaking assertion. If someone later adds
a `messages` table or a `dek` column to the control plane, CI stops them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from control_api.db import get_engine

# Substrings that must never appear in a control-plane column name.
FORBIDDEN_COLUMN_SUBSTRINGS = [
    "message",
    "conversation",
    "chat_text",
    "transcript",
    "body",
    "content",
    "prompt",
    "completion",
    "password",
    "anthropic",
    "openai",
    "api_key",
    "apikey",
    "oauth",
    "refresh_token",
    "access_token",
    "dek",  # the tenant DEK is a Railway variable only -- never a DB column
]

# Tables the control plane is allowed to own at all.
ALLOWED_TABLES = {"tenant", "bot", "provisionjob"}


def test_only_expected_tables_exist():
    tables = set(inspect(get_engine()).get_table_names())
    assert tables == ALLOWED_TABLES, f"unexpected control-plane tables: {tables ^ ALLOWED_TABLES}"


@pytest.mark.parametrize("table", sorted(ALLOWED_TABLES))
def test_no_column_can_hold_content_or_user_credentials(table):
    inspector = inspect(get_engine())
    for column in inspector.get_columns(table):
        name = column["name"].lower()
        for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
            # `dek_set` is a boolean flag, explicitly permitted by the task spec.
            if forbidden == "dek" and name == "dek_set":
                continue
            assert forbidden not in name, (
                f"{table}.{name} looks like it could hold conversation content or a "
                f"plaintext user credential (matched '{forbidden}'). "
                "The control plane must be structurally unable to."
            )


# Whitelist, not blacklist -- the strongest form of this guard. Every control-plane
# column is enumerated here, so adding one is a deliberate, reviewed act.
EXPECTED_COLUMNS = {
    "tenant": {
        "id",
        "email",
        "status",
        "bot_id",
        "railway_service_id",
        "railway_service_name",
        "railway_volume_id",
        "internal_url",
        "dek_set",
        "trial_key_alias",
        "trial_key_active",
        "webhook_set",
        "created_at",
        "updated_at",
    },
    # `token` and `webhook_secret` are OUR pool-bot credentials (BotFather +
    # self-generated), not the user's -- control-api cannot call setWebhook without
    # them. No user credential belongs in this table.
    "bot": {
        "id",
        "token",
        "username",
        "status",
        "webhook_secret",
        "assigned_tenant_id",
        "created_at",
    },
    # `last_error` carries third-party error text, which is why it is scrubbed
    # through `provisioning._redact` before it is ever persisted.
    "provisionjob": {
        "id",
        "tenant_id",
        "step",
        "status",
        "attempts",
        "max_attempts",
        "last_error",
        "next_attempt_at",
        "created_at",
        "updated_at",
    },
}


@pytest.mark.parametrize("table", sorted(ALLOWED_TABLES))
def test_table_holds_only_the_whitelisted_columns(table):
    columns = {c["name"] for c in inspect(get_engine()).get_columns(table)}
    assert columns == EXPECTED_COLUMNS[table], (
        f"{table} columns changed; if this is intentional, confirm the new column "
        "cannot hold conversation content or a plaintext user credential, then "
        "update EXPECTED_COLUMNS."
    )
