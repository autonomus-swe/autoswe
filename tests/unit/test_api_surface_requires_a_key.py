"""Every route on the app is either guarded by a key or publicly readable on purpose.

`docs/security.md` §8 leads with *"Every route requires a key"* and cites one test:
`tests/integration/test_api.py::test_create_run_requires_a_valid_key`, which checks
`POST /runs`. Grepping the whole test tree for a 401 finds five paths — `POST /runs`,
`GET /runs`, `GET /runs/{id}/artifacts`, `…/artifacts/{kind}` and the MCP transport. The
other six routes were never asked for a key by anything, and two mutations survived the
whole suite:

| mutation | consequence |
|---|---|
| drop `Depends(rate_limit)` from `/runs/{id}/answer` | **anyone can answer a parked run** |
| `key or request.query_params.get("key")` → `key` | the console's event stream cannot connect |

The first is the serious one and it is invisible from both ends: the route still returns
202, the answer still reaches the run, the console still works. What changes is who may
send it — and `/answer`, `/approve` and `/reject` are precisely the three routes where the
caller is standing in for a human deciding whether a tool call may run. An unauthenticated
`/approve` is a stranger authorising a command inside somebody else's sandbox.

The second is the mirror image: §8 documents the query-parameter fallback as a *deliberate*
exception, because a browser cannot set headers on an `EventSource`. Nothing in `tests/`
has ever sent `?key=`; `api/static/app.js` is the only caller. So the one route whose
authentication path is written down as special is the one route whose authentication path
was never exercised.

## Why this is a classification rather than six more tests

Six one-off tests would have closed those six routes and nothing else. The failure mode
here is not a wrong decision but an absent one — nobody chose to leave `/approve`
unguarded; the route was added and the question was never asked. So the app's own route
table is walked and **every entry must appear in one of three tables**: guarded by the
header, guarded at the ASGI layer, or public with a reason written beside it. Adding a
route then fails this file until somebody decides which it is.

## §8's claim is literally false, and the exceptions are the interesting part

`/healthz`, `/metrics`, `/`, `/ui/*` and FastAPI's own `/docs`, `/redoc` and
`/openapi.json` require no key, each for a reason that is good and none of which §8
mentions. "Every route requires a key" is a summary, not a fact; the `PUBLIC` table below
is the fact. Writing the exceptions down is worth more than the slogan, because the next
unauthenticated route gets added by somebody who read the slogan.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import auth
from api.main import create_app
from api.routes.mcp_mount import RequireApiKey
from core.settings import get_settings, load_settings

pytestmark = pytest.mark.unit

# Deliberately unlike the integration suite's "test-key-123456" and unlike anything a
# default could produce: a key that matched by coincidence would prove nothing. Two of
# them, because `API_KEYS` is a comma-separated set and a guard that only ever consults
# the first one would pass a single-key test.
KEY = "stream-key-9f2d41c7"
SECOND_KEY = "console-key-4b7e03a9"

# Not the all-zero UUID the integration suite uses for "missing", so a failure here cannot
# be confused with one there. No run by this id exists; nothing in this file gets far
# enough to look, because rejecting the key happens before the handler runs.
RUN_ID = "7f3c9a21-0000-4000-8000-0d1e2f3a4b5c"
PATH_PARAMS = {"run_id": RUN_ID, "kind": "diff"}

# A prefix and an extension of a real key are in here on purpose: those are the guesses an
# attacker makes, and they are the ones a sloppy comparison accepts.
WRONG_KEYS = (
    KEY[:-1],
    KEY + "0",
    KEY.upper(),
    "not-a-configured-key-at-all",
)

# The two terminal authentication dependencies. Everything else in `api/auth.py` —
# `rate_limit`, `read_rate_limit` — reaches one of these, and that is what makes walking
# the dependency tree meaningful rather than a check on one layer's spelling.
HEADER = auth.require_api_key
HEADER_OR_QUERY = auth.require_api_key_or_query

# Every guarded route, and which of the two it must end at. The value is load-bearing in
# both directions: `HEADER_OR_QUERY` on a write route would put an operator's key into
# every access log and browser history between here and the client, and `HEADER` on the
# event stream would break the console with no server-side error at all.
AUTHENTICATED: dict[str, Any] = {
    "POST /runs": HEADER,
    "GET /runs": HEADER,
    "GET /runs/{run_id}": HEADER,
    "GET /runs/{run_id}/detail": HEADER,
    "GET /runs/{run_id}/artifacts": HEADER,
    "GET /runs/{run_id}/artifacts/{kind}": HEADER,
    "POST /runs/{run_id}/answer": HEADER,
    "POST /runs/{run_id}/approve": HEADER,
    "POST /runs/{run_id}/reject": HEADER,
    "POST /runs/{run_id}/cancel": HEADER,
    # The documented exception, and the only one. See `api/auth.require_api_key_or_query`.
    "GET /runs/{run_id}/events": HEADER_OR_QUERY,
}

# The MCP transport is a separate ASGI application and FastAPI's dependency tree stops at
# that boundary, so its guard is a wrapper rather than a `Depends`. Checked structurally
# below; the behaviour over a live session is
# `tests/integration/test_mcp_server.py::test_the_http_transport_refuses_an_unknown_key`.
ASGI_GUARDED = frozenset(
    {
        "GET /mcp",
        "POST /mcp",
        "DELETE /mcp",
        "GET /mcp/",
        "POST /mcp/",
        "DELETE /mcp/",
    }
)

# Unauthenticated on purpose. Each reason has to survive being read by somebody deciding
# whether their new route belongs here, so "it is only a read" is not one of them.
PUBLIC: dict[str, str] = {
    "GET /healthz": "liveness must answer when the keys themselves are misconfigured",
    "GET /metrics": "a Prometheus scraper is infrastructure and cannot hold a key; every "
    "label is a role or an outcome and none names a repository, goal or customer",
    "GET /": "the console's HTML shell, which asks for the key and then uses it",
    "MOUNT /ui": "the console's static JavaScript and CSS, served before anyone has a key",
    "GET /openapi.json": "the shape of the API, not its data; serving it needs no run",
    "GET /docs": "FastAPI's Swagger page, which renders the schema above",
    "GET /docs/oauth2-redirect": "Swagger's own callback stub, part of the docs page",
    "GET /redoc": "the second schema renderer FastAPI installs beside Swagger",
}

# A mount has no method and no file of its own, so it needs a concrete request to prove
# anything. `/ui` alone is a 404 from StaticFiles; `/ui/app.js` is what a browser asks for.
MOUNT_PROBE: dict[str, tuple[str, str]] = {"MOUNT /ui": ("GET", "/ui/app.js")}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """The real app, built the way `tests/integration/conftest.api_app` builds it.

    `env_file=None` keeps a developer's `.env` out of the test, and the environment is set
    rather than the settings object patched because the routes resolve `Depends(get_settings)`
    at request time. `get_settings` is an `lru_cache`, so it is cleared on both sides — a
    cached `Settings` carrying this file's keys leaking into another test would be a
    confusing way to find out.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("API_KEYS", f"{KEY},{SECOND_KEY}")
    get_settings.cache_clear()
    try:
        yield create_app(load_settings(env_file=None))
    finally:
        get_settings.cache_clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """No lifespan: nothing here gets past the key check, so nothing needs a database.

    `raise_server_exceptions=False` because the routes that *do* accept the key then reach
    for `app.state.engine` and fail — which is the correct outcome for a unit test and has
    to come back as a status code rather than an exception for the counterweight below to
    be able to say "not a 401".
    """
    return TestClient(app, raise_server_exceptions=False)


def walk(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Every leaf route, with its full path.

    FastAPI no longer copies an included router's routes onto the app; it appends a lazy
    holder that keeps the original router and the prefix it was included under. Recursing
    through `original_router` by duck-typing rather than importing the private class, so a
    FastAPI that goes back to flattening still walks correctly — and so an app whose routes
    had quietly stopped being visible here would show up as an empty surface rather than as
    a classification that passes because it has nothing to classify.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is None:
            yield prefix + str(getattr(route, "path", "")), route
            continue
        context = getattr(route, "include_context", None)
        yield from walk(inner.routes, prefix + str(getattr(context, "prefix", "")))


def surface(app: FastAPI) -> dict[str, Any]:
    """The app's routes keyed as `"METHOD /path"`, one entry per method.

    `HEAD` is dropped because Starlette adds it to every `GET` and a classification table
    listing both would be twice as long and say the same thing.
    """
    found: dict[str, Any] = {}
    for path, route in walk(list(app.routes)):
        methods = sorted(set(getattr(route, "methods", None) or []) - {"HEAD"})
        for key in [f"{m} {path}" for m in methods] or [f"MOUNT {path}"]:
            found[key] = route
    return found


def probe(key: str) -> tuple[str, str]:
    """A classification key as a request anyone can actually send."""
    if key in MOUNT_PROBE:
        return MOUNT_PROBE[key]
    method, _, template = key.partition(" ")
    return method, template.format(**PATH_PARAMS)


def dependency_calls(dependant: Any) -> set[Any]:
    """Every callable in a route's dependency tree, however deep.

    Flattened rather than checked one layer down, because `rate_limit` and
    `read_rate_limit` are both wrappers — a check on the immediate dependency would be a
    check on which bucket the route uses, not on whether it authenticates at all.
    """
    found: set[Any] = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= dependency_calls(sub)
    return found


# ---- the classification -------------------------------------------------------------------


def test_every_route_the_app_serves_is_classified(app: FastAPI) -> None:
    """Adding a route forces somebody to decide whether it needs a key.

    This is the test that closes the class rather than the instance. The surviving mutation
    was not a wrong decision about `/answer` — it was an absent one: the route was written,
    it worked, and nothing ever asked who was allowed to call it. A new route gets added the
    same way.
    """
    classified = set(AUTHENTICATED) | ASGI_GUARDED | set(PUBLIC)
    unclassified = set(surface(app)) - classified

    assert unclassified == set(), (
        f"new routes {sorted(unclassified)}: add each to AUTHENTICATED (naming the auth "
        "dependency it must reach), to ASGI_GUARDED, or to PUBLIC with the reason it needs "
        "no key"
    )


def test_the_surface_is_not_empty_and_the_tables_do_not_overlap(app: FastAPI) -> None:
    """The counterweight for the test above, which an empty surface would satisfy.

    `walk` recurses through a FastAPI internal. If that internal is renamed the recursion
    silently returns the handful of top-level routes, the unclassified set comes back empty,
    and this file would go on passing while checking nothing. So the count is pinned from
    below, and a route cannot be called both guarded and public.
    """
    found = surface(app)

    assert len(found) >= len(AUTHENTICATED) + len(ASGI_GUARDED) + len(PUBLIC), (
        f"only {len(found)} routes were found; `walk` has stopped recursing into included "
        "routers, so every classification assertion in this file is now vacuous"
    )
    assert set(AUTHENTICATED).isdisjoint(ASGI_GUARDED), "a route cannot be guarded twice over"
    assert set(AUTHENTICATED).isdisjoint(PUBLIC), "a route cannot be both guarded and public"
    assert ASGI_GUARDED.isdisjoint(PUBLIC), "a route cannot be both guarded and public"


def test_no_classification_outlives_the_route_it_describes(app: FastAPI) -> None:
    """The other direction: a renamed path must not leave an entry guarding nothing.

    Without this, renaming `/runs/{run_id}/approve` would drop its 401 test in silence —
    every parametrised case below would still pass, against a route that no longer exists.
    """
    stale = (set(AUTHENTICATED) | ASGI_GUARDED | set(PUBLIC)) - set(surface(app))

    assert stale == set(), f"classified but not served: {sorted(stale)}"


# ---- the key is demanded --------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(AUTHENTICATED))
def test_a_guarded_route_refuses_a_request_carrying_no_key(route: str, client: TestClient) -> None:
    """Parametrised so a failure names the route rather than "the API".

    "a route stopped requiring a key" is true of every one of these; "`/approve` stopped
    requiring a key" is the one that gets fixed before anything else. The body is pinned as
    well as the status, because a 401 that leaks which key was expected, or which route
    exists, is a different answer from the generic one `api/auth.UNAUTHORIZED` gives.
    """
    method, path = probe(route)

    res = client.request(method, path)

    assert res.status_code == 401, f"{route} answered {res.status_code} with no key at all"
    assert res.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize("route", sorted(AUTHENTICATED))
def test_a_guarded_route_refuses_every_wrong_key(route: str, client: TestClient) -> None:
    """A guard that accepts the header's mere presence is no guard.

    One of these guesses is a real key with its last character removed and another is a real
    key with one added, so a comparison that checks a prefix, a length or a truthy header
    fails here rather than in production.
    """
    method, path = probe(route)

    for wrong in WRONG_KEYS:
        res = client.request(method, path, headers={"X-API-Key": wrong})
        assert res.status_code == 401, f"{route} accepted {wrong!r}"


@pytest.mark.parametrize("key", [KEY, SECOND_KEY])
@pytest.mark.parametrize("route", sorted(AUTHENTICATED))
def test_a_guarded_route_lets_a_configured_key_through(
    route: str, key: str, client: TestClient
) -> None:
    """The counterweight, without which `raise UNAUTHORIZED` unconditionally passes the two
    tests above and takes the product down.

    "Not a 401" rather than a specific success, deliberately: no database is running here,
    so a route that accepts the key then answers 422 for a missing body or 500 for an absent
    engine. Either proves the only thing this test claims — the key was accepted and the
    request reached the handler. What the handler then does is every other test in the suite.

    Both configured keys are tried, because `API_KEYS` is a set and a guard that compares
    against only the first member would otherwise look correct.
    """
    method, path = probe(route)

    res = client.request(method, path, headers={"X-API-Key": key})

    assert res.status_code != 401, f"{route} refused the configured key {key!r}"


@pytest.mark.parametrize("route", sorted(AUTHENTICATED))
def test_a_guarded_route_declares_the_auth_dependency_it_is_supposed_to(
    route: str, app: FastAPI
) -> None:
    """The structural half, which catches what a status code cannot: the *wrong* guard.

    Swapping a write route's `rate_limit` for `require_api_key_or_query` keeps every 401
    assertion above green while making `POST /runs/{id}/approve?key=…` work — and a key in
    a URL is a key in the access log, the browser history and every proxy in between. The
    inverse is just as quiet: putting the header-only dependency on the event stream leaves
    the server perfectly healthy and the console permanently unable to connect.

    So each route names its terminal dependency and the other one must be absent.
    """
    expected = AUTHENTICATED[route]
    found = surface(app)[route]
    assert isinstance(found, APIRoute), f"{route} is no longer a FastAPI route"

    calls = dependency_calls(found.dependant)
    forbidden = HEADER_OR_QUERY if expected is HEADER else HEADER

    assert expected in calls, (
        f"{route} does not reach {expected.__name__}; its dependency tree is "
        f"{sorted(getattr(c, '__name__', repr(c)) for c in calls)}"
    )
    assert forbidden not in calls, (
        f"{route} reaches {forbidden.__name__}, which is the wrong guard for it"
    )


# ---- the one documented exception -----------------------------------------------------------


def test_the_event_stream_accepts_its_key_as_a_query_parameter(client: TestClient) -> None:
    """The console's only way in, and nothing has ever tested it.

    A browser cannot set headers on an `EventSource`, so `api/static/app.js` sends
    `/runs/<id>/events?key=…`. Dropping the fallback — one `or` — leaves the whole suite
    green and the live console stuck on a 401 that no server-side error explains, because
    from the API's point of view nothing went wrong: a request arrived without a key and was
    refused, exactly as designed.
    """
    res = client.get(f"/runs/{RUN_ID}/events?key={KEY}")

    assert res.status_code != 401, (
        "the event stream refused a valid key in the query string; the console cannot send "
        "it any other way"
    )


@pytest.mark.parametrize("wrong", [*WRONG_KEYS, ""])
def test_the_event_stream_refuses_a_wrong_key_in_the_query_string(
    wrong: str, client: TestClient
) -> None:
    """So the fallback cannot quietly become "accept anything with a `key` in the URL".

    The empty string is in here because `candidate = key or query` makes an empty value
    falsy twice over, and "no key at all" and "a key that is the empty string" must land on
    the same answer.
    """
    res = client.get(f"/runs/{RUN_ID}/events?key={wrong}")

    assert res.status_code == 401, f"the event stream accepted {wrong!r} from the query string"


def test_the_event_stream_reads_the_key_parameter_and_not_merely_a_query_string(
    client: TestClient,
) -> None:
    """A real key under the wrong parameter name is still no key.

    Without this, "is there anything in the query string" would pass the test above; the
    claim is that one named parameter is consulted, which is what the console sends and what
    `docs/security.md` §8 describes.
    """
    res = client.get(f"/runs/{RUN_ID}/events?apikey={KEY}&token={KEY}")

    assert res.status_code == 401, "a key under another parameter name was accepted"


@pytest.mark.parametrize("route", sorted(r for r in AUTHENTICATED if AUTHENTICATED[r] is HEADER))
def test_no_route_but_the_event_stream_takes_its_key_from_the_url(
    route: str, client: TestClient
) -> None:
    """§8: *"deliberately limited to the read-only event stream"*.

    Query strings end up in access logs, referrer headers and browser history, so a key that
    rides in one has effectively been written down. The exception is worth its cost for a
    read-only stream a browser has no other way to open; it is not worth it for `/approve`,
    where the URL in a log line would be a standing authorisation to run somebody's tool
    call.
    """
    method, path = probe(route)
    separator = "&" if "?" in path else "?"

    res = client.request(method, f"{path}{separator}key={KEY}")

    assert res.status_code == 401, f"{route} accepted a key from the query string"


# ---- the transport whose guard is not a dependency ------------------------------------------


@pytest.mark.parametrize("route", sorted(ASGI_GUARDED))
def test_the_mcp_transport_is_wrapped_rather_than_merely_routed(route: str, app: FastAPI) -> None:
    """FastAPI's dependency tree stops at a sub-application's boundary.

    A `Depends(require_api_key)` on the router that attaches the MCP app is never consulted
    for requests inside it, so attaching it without noticing would publish every tool —
    `cancel_run` included — unauthenticated, while the routes beside it stayed guarded and
    the mistake looked like nothing at all. The guard therefore has to be the wrapper, and
    that is a fact about the object rather than about a response.
    """
    found = surface(app)[route]
    endpoint = getattr(found, "endpoint", None)

    assert isinstance(endpoint, RequireApiKey), (
        f"{route} is served by {type(endpoint).__name__} rather than the ASGI key guard; a "
        "dependency on the attaching router would not be consulted inside the MCP app"
    )
    assert endpoint.keys == frozenset({KEY, SECOND_KEY}), "and it holds the configured keys"


def test_the_mcp_transport_refuses_a_request_with_no_key(client: TestClient) -> None:
    """The wrapper, over a real request rather than by inspection."""
    res = client.post("/mcp")

    assert res.status_code == 401
    assert res.json() == {"detail": "unauthorized"}


def test_the_mcp_guard_admits_a_configured_key_and_refuses_the_near_misses(
    app: FastAPI,
) -> None:
    """The counterweight for the wrapper, asserted against the guard rather than the route.

    A live authorised request would reach the MCP session manager, which this app has not
    started, so it would answer 500 for a reason that has nothing to do with the key — a
    counterweight that cannot tell "accepted" from "broken" is not one. The guard's own
    decision is the claim, so the guard is what is asked.
    """
    endpoint = surface(app)["POST /mcp"].endpoint
    assert isinstance(endpoint, RequireApiKey)

    def decide(sent: bytes | None) -> bool:
        headers = [] if sent is None else [(b"x-api-key", sent)]
        return endpoint._authorised({"type": "http", "headers": headers})

    assert decide(KEY.encode()) is True
    assert decide(SECOND_KEY.encode()) is True, "the second configured key is a key too"
    assert decide(None) is False
    for wrong in WRONG_KEYS:
        assert decide(wrong.encode()) is False, f"the MCP guard accepted {wrong!r}"


# ---- the routes that need no key, and why ---------------------------------------------------


@pytest.mark.parametrize("route", sorted(PUBLIC))
def test_a_public_route_answers_without_a_key(route: str, client: TestClient) -> None:
    """The exceptions §8's "every route requires a key" does not mention.

    Each is defensible and none is accidental — a scraper cannot hold a key, liveness has to
    answer when the keys are the thing that is misconfigured, and the console's JavaScript is
    what asks the operator for a key in the first place. Asserted so that a guard added here
    by a well-meaning reading of §8 is noticed on the way in rather than when Prometheus goes
    blind.

    Only "not a 401": `/healthz` reports 503 without a database, which is its answer to a
    question that was asked and reached it.
    """
    method, path = probe(route)

    res = client.request(method, path)

    assert res.status_code != 401, f"{route} now demands a key; {PUBLIC[route]}"
