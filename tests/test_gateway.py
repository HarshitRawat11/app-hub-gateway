"""Tests for the gateway proxy and its failure mapping.

Run from gateway/:

    uv run pytest -v

`links-service` is never started. Every upstream response is faked with
`httpx2.MockTransport`, which swaps out the transport layer underneath the real
`AsyncClient` -- so the client, the await, the timeout config and the exception
handling are all the genuine article, but nothing touches a socket.

That is what makes the 502 and 504 paths testable at all. Previously they
needed `tests/fake_upstream.py` running on a spare port, which is fine for a
one-off check and useless in CI.

Both services are on `httpx2` as of 2026-09-10 (D-17). gateway moved while it
was still undeployed, which was the cheapest moment: its whole surface is one
AsyncClient, one get and three exception types, and every name it uses is
identical in the new package.
"""

import contextlib
import importlib

import httpx2
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app

# `id` is a UUID string, not an int, since C-06. gateway never parses it --
# it forwards whatever links-service sends -- so this fixture would have gone
# on passing with the old shape while describing an API that no longer exists.
# Exactly the kind of quietly-wrong claim this project keeps finding.
UPSTREAM_PAYLOAD = [
    {"id": "7c9f4b1e-2a6d-4f88-9b0c-1d3e5a7f2c40", "name": "n8n",
     "url": "http://localhost:5678", "category": "tools", "icon": "workflow"}
]


@contextlib.contextmanager
def gateway_with_upstream(handler):
    """Run gateway with its HTTP client wired to a fake upstream.

    `TestClient` as a context manager is what triggers the app's `lifespan`,
    which is where the real `AsyncClient` gets created. We then replace it --
    after startup, before any request -- with one whose transport is a mock.

    Without the `with`, `lifespan` never runs, `app.state.http_client` never
    exists, and every test fails with AttributeError rather than anything
    informative.

    `main.app` is resolved HERE, on every call, rather than using the `app`
    imported at the top of this file. The two config tests below call
    `importlib.reload(main)`, and reload re-executes the module into the SAME
    namespace dict -- so `main.app` is rebound to a fresh FastAPI instance
    while this file's imported `app` still points at the old one. The route
    handlers look up `app` in the module namespace at call time, so they would
    then read the NEW app's state while this helper wrote to the OLD one, and
    every request would fail with exactly the AttributeError described above.

    It went unnoticed while this was the only test file, because the reload
    tests run last. `test_proxy_crud.py` sorts after it and found it at once.
    """
    current = main.app
    with TestClient(current) as client:
        real = current.state.http_client
        current.state.http_client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), timeout=3.0
        )
        try:
            yield client
        finally:
            current.state.http_client = real


def responds(status, json=None):
    """An upstream that answers with a given status."""
    def handler(request):
        return httpx2.Response(status, json=json if json is not None else {})
    return handler


def raises(exc):
    """An upstream that fails the way httpx2 would fail."""
    def handler(request):
        raise exc
    return handler


# --------------------------------------------------------------- happy path --

def test_links_passes_the_upstream_body_through():
    with gateway_with_upstream(responds(200, UPSTREAM_PAYLOAD)) as client:
        r = client.get("/links")
    assert r.status_code == 200
    assert r.json() == UPSTREAM_PAYLOAD


def test_gateway_requests_the_links_path_on_the_configured_base():
    """The base URL is config; `/links` is part of the contract and is not."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx2.Response(200, json=[])

    with gateway_with_upstream(handler) as client:
        client.get("/links")

    assert seen["url"] == f"{main.LINKS_SERVICE_URL}/links"


# ---------------------------------------------------------- failure mapping --

def test_unreachable_upstream_maps_to_503():
    with gateway_with_upstream(raises(httpx2.ConnectError("refused"))) as client:
        r = client.get("/links")
    assert r.status_code == 503
    assert r.json() == {"detail": "links-service unavailable"}


def test_slow_upstream_maps_to_504():
    """`TimeoutException` is a SUBCLASS of `RequestError`.

    So this test is also what proves the `except` clauses are ordered
    correctly. Put `RequestError` first and it swallows timeouts too, this
    returns 503, and you would never see a 504 in production either.
    """
    with gateway_with_upstream(raises(httpx2.ReadTimeout("too slow"))) as client:
        r = client.get("/links")
    assert r.status_code == 504
    assert r.json() == {"detail": "links-service timed out"}


@pytest.mark.parametrize("upstream_status", [400, 404, 500, 503])
def test_upstream_error_status_maps_to_502(upstream_status):
    """A 4xx/5xx from upstream is a *successful* HTTP exchange.

    httpx2 raises nothing -- the connection opened, the request went, a
    well-formed response came back. So this branch cannot be an `except`; it
    has to be an explicit status check, and it has to run before `.json()`.
    """
    with gateway_with_upstream(responds(upstream_status, {"detail": "nope"})) as client:
        r = client.get("/links")
    assert r.status_code == 502
    assert r.json() == {"detail": "links-service returned an error"}


def test_non_json_error_body_still_maps_to_502():
    """Regression guard for the status check running before `.json()`.

    An upstream 500 with an HTML body once produced a 500 from gateway -- not
    from the error handling, but from `.json()` raising JSONDecodeError on
    `<html>`. Right answer, wrong reason, and it hid the missing 502 entirely.
    If the status check is ever moved below `.json()`, this test fails.
    """
    def handler(request):
        return httpx2.Response(500, text="<html><body>500</body></html>",
                              headers={"Content-Type": "text/html"})

    with gateway_with_upstream(handler) as client:
        r = client.get("/links")
    assert r.status_code == 502


# ------------------------------------------------------- information leakage --

@pytest.mark.parametrize("handler,expected", [
    (raises(httpx2.ConnectError("boom")), 503),
    (raises(httpx2.ReadTimeout("boom")), 504),
    (responds(500), 502),
])
def test_error_responses_never_leak_the_upstream_address(handler, expected):
    """httpx2 puts the attempted URL in its exception message.

    Passing `str(e)` through as `detail` handed the caller
    `http://links-service:8000/links` -- internal topology, to anyone curling
    the public endpoint. The details are fixed strings now; this stops that
    regressing.
    """
    with gateway_with_upstream(handler) as client:
        r = client.get("/links")
    assert r.status_code == expected
    body = r.text
    for leak in ("localhost", "links-service:8000", "http://", "8000"):
        assert leak not in body


# ------------------------------------------------------------------- health --

def test_health_does_not_depend_on_the_upstream():
    """The single most important test in this file.

    `/health` backs the liveness probe. If it checked `links-service`, then
    `links-service` going down would get *gateway* killed and restarted by
    Kubernetes -- one outage becoming two, with the restart noise burying the
    real cause. So: upstream is hard down here, and /health must still be 200.
    """
    with gateway_with_upstream(raises(httpx2.ConnectError("upstream is gone"))) as client:
        assert client.get("/links").status_code == 503   # upstream really is down
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ------------------------------------------------------------------- config --

def test_links_service_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("LINKS_SERVICE_URL", raising=False)
    reloaded = importlib.reload(main)
    try:
        assert reloaded.LINKS_SERVICE_URL == "http://localhost:8000"
    finally:
        monkeypatch.undo()
        importlib.reload(main)


def test_links_service_url_strips_a_trailing_slash(monkeypatch):
    """A trailing slash would otherwise produce `...8000//links`.

    Some servers tolerate that, some 404, and it is miserable to debug because
    the URL looks correct in every log line.
    """
    monkeypatch.setenv("LINKS_SERVICE_URL", "http://links-service:8000/")
    reloaded = importlib.reload(main)
    try:
        assert reloaded.LINKS_SERVICE_URL == "http://links-service:8000"
    finally:
        monkeypatch.undo()
        importlib.reload(main)
