"""The same public RPCs can be called over REST or a PostgreSQL connection."""

import json
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .db import PROJECT_REF, check_identity, connect, settings


class RestClient:
    def __init__(self, url: str, secret: str):
        if url.rstrip("/") != f"https://{PROJECT_REF}.supabase.co":
            raise ValueError("REST URL must match the dedicated Supabase project")
        if not secret:
            raise ValueError("Set SUPABASE_SECRET_KEY for REST calls")
        self.url = url.rstrip("/")
        self._secret = secret

    def rpc(self, function: str, arguments: dict):
        if not function.replace("_", "").isalnum():
            raise ValueError("Invalid RPC name")
        request = Request(
            f"{self.url}/rest/v1/rpc/{function}",
            data=json.dumps(arguments).encode("utf-8"),
            headers={"apikey": self._secret, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("RPC not installed; apply supabase/bootstrap.sql first") from None
            raise ValueError(f"Supabase RPC failed with HTTP {exc.code}") from None
        except URLError:
            raise ValueError("Could not connect to the dedicated Supabase REST endpoint") from None


def rpc(client, function: str, arguments: dict):
    if not isinstance(client, psycopg.Connection):
        return client.rpc(function, arguments)
    casts = {
        "p_commander_ids": "uuid[]",
        "p_date_from": "date",
        "p_date_to": "date",
        "p_card_id": "uuid",
        "p_oracle_id": "uuid",
    }
    parameters = []
    values = []
    for key, value in arguments.items():
        cast = sql.SQL("::" + casts[key]) if key in casts else sql.SQL("")
        parameters.append(sql.SQL("{} => %s{}").format(sql.Identifier(key), cast))
        values.append(Jsonb(value) if key in {"p_cards", "p_deck", "p_decks"} else value)
    statement = sql.SQL("select public.{}({})").format(
        sql.Identifier(function), sql.SQL(",").join(parameters)
    )
    return client.execute(statement, values).fetchone()[0]


@contextmanager
def data_client():
    config = settings()
    if config["secret"]:
        yield RestClient(config["url"], config["secret"])
    else:
        with connect() as conn:
            check_identity(conn)
            yield conn
