"""Telegram ids must survive a 64-bit world.

Found live: `POST /internal/bots` with real BotFather tokens died with psycopg
`NumericValueOutOfRange: integer out of range`. Telegram now issues 10-digit bot
ids (>2^31) and SQLModel's plain `int` maps to a 32-bit INTEGER on Postgres.

The suite was green throughout, because SQLite has no fixed-width integer types --
it stores whatever you give it. So behavioural tests **cannot** catch this class of
bug on SQLite, and a passing round-trip below proves nothing about Postgres. The
load-bearing assertions here are therefore the *type* assertions: they pin the
declared column type, which is what actually determines the DDL Postgres gets.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import BigInteger
from sqlmodel import select

from control_api import db
from control_api.models import Bot, Tenant

# A real-shaped modern Telegram bot id: 10 digits, comfortably over 2^31-1
# (2_147_483_647), which is exactly what overflowed a Postgres INTEGER.
BIG_BOT_ID = 7_651_902_344
assert BIG_BOT_ID > 2**31 - 1

#: Every column that holds a Telegram-issued numeric id.
#: Heartbeat is deliberately absent -- it holds no chat/user ids by design
#: (see its docstring); its integers are counters and gauges.
TELEGRAM_ID_COLUMNS = [
    (Bot, "id"),
    (Tenant, "bot_id"),
]


@pytest.mark.parametrize(
    ("model", "column_name"),
    TELEGRAM_ID_COLUMNS,
    ids=[f"{m.__name__}.{c}" for m, c in TELEGRAM_ID_COLUMNS],
)
def test_telegram_id_columns_are_big_integers(model, column_name):
    """Pin the declared type. This is the assertion that would have caught it."""
    column = model.__table__.columns[column_name]
    assert isinstance(column.type, BigInteger), (
        f"{model.__name__}.{column_name} is {column.type!r}, not BigInteger. "
        "Telegram ids exceed 2^31; a 32-bit INTEGER column raises "
        "NumericValueOutOfRange on Postgres the first time a real bot is registered."
    )


def test_the_foreign_key_matches_the_primary_key_width():
    """A 32-bit FK pointing at a 64-bit PK fails to create the constraint on
    Postgres, so these two must be changed together or not at all."""
    pk = Bot.__table__.columns["id"]
    fk = Tenant.__table__.columns["bot_id"]
    assert type(pk.type) is type(fk.type)


def test_generated_ddl_uses_bigint():
    """Belt and braces: check what Postgres would actually be sent.

    Compiling against the Postgres dialect (without connecting) is the closest we
    can get to the real DDL from a SQLite-only test environment.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(Bot.__table__).compile(dialect=postgresql.dialect()))
    # Plain BIGINT, not BIGSERIAL: the id is assigned by Telegram, so there should
    # be no sequence. Either would be 64-bit and fix the overflow, but BIGSERIAL
    # would mean SQLAlchemy thinks it generates these ids.
    assert "id BIGINT NOT NULL" in ddl, ddl
    assert "SERIAL" not in ddl.upper(), ddl

    ddl = str(CreateTable(Tenant.__table__).compile(dialect=postgresql.dialect()))
    assert "bot_id BIGINT" in ddl, ddl


@respx.mock
def test_a_modern_bot_id_survives_the_whole_api_path(client, auth):
    """End-to-end on the real path: register a >2^31 bot, provision against it, and
    resolve it back through the ingress hot-path lookup.

    On SQLite this cannot fail for range reasons -- it is here to prove nothing
    ELSE truncates or rejects a 10-digit id along the way (path params, JSON
    round-trips, the token->id parse, the by-bot query).
    """
    token = f"{BIG_BOT_ID}:AA-real-shaped-secret"
    respx.post(f"https://api.telegram.org/bot{token}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"id": BIG_BOT_ID, "username": "big_bot"}}
        )
    )

    registered = client.post("/internal/bots", json={"tokens": [token]}, headers=auth)
    assert registered.status_code == 200, registered.text
    assert registered.json()["registered"] == [
        {"bot_id": BIG_BOT_ID, "username": "big_bot"}
    ]

    with db.session_scope() as s:
        bot = s.get(Bot, BIG_BOT_ID)
        assert bot is not None and bot.id == BIG_BOT_ID

    # Assign it to a tenant the way provisioning does, then resolve it back.
    with db.session_scope() as s:
        s.add(
            Tenant(
                id="t-big",
                email="big@squire.test",
                bot_id=BIG_BOT_ID,
                internal_url="http://tenant-t-big.railway.internal:8080",
            )
        )
        bot = s.get(Bot, BIG_BOT_ID)
        bot.assigned_tenant_id = "t-big"
        s.add(bot)
        s.commit()

    found = client.get(f"/internal/tenants/by-bot/{BIG_BOT_ID}", headers=auth)
    assert found.status_code == 200, found.text
    assert found.json()["tenant_id"] == "t-big"

    # The id survived JSON and the URL path without being truncated or stringified
    # into something lossy.
    with db.session_scope() as s:
        tenant = s.exec(select(Tenant).where(Tenant.bot_id == BIG_BOT_ID)).first()
        assert tenant is not None and tenant.bot_id == BIG_BOT_ID
