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


def test_tenant_row_holds_only_registry_metadata():
    """Whitelist, not blacklist -- the strongest form of this guard."""
    columns = {c["name"] for c in inspect(get_engine()).get_columns("tenant")}
    assert columns == {
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
    }
