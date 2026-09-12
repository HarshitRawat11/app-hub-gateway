"""The full CRUD proxy, and the passthrough rule that makes it correct.

`test_gateway.py` covers `GET /links` and the failure mapping that S-01 steps
1-3 established. This file covers what S-03 needed on top of it: the other
three routes, and the one genuinely new idea -- that some upstream 4xx
responses are the CALLER's answer and must not be flattened into a 502.

The helpers come from `test_gateway`. pytest puts `tests/` on `sys.path`
(there is no `__init__.py`, so that directory is the import base), which makes
the import work without a package. Sharing them beats a second copy of the
same ten lines drifting out of step.
"""

import httpx2
import pytest

from test_gateway import gateway_with_upstream, raises, responds

LINK = {"id": "7c9f4b1e-2a6d-4f88-9b0c-1d3e5a7f2c40", "name": "n8n",
        "url": "http://localhost:5678", "category": "tools", "icon": "workflow"}

NEW = {"name": "grafana", "url": "http://localhost:3000", "category": "obs"}


def router(routes, default=(404, {"detail": "Link not found"})):
    """A fake links-service that answers by (method, path).

    Anything not in `routes` gets `default`, which is deliberately a 404 with
    the real links-service detail string -- that is the case most of these
    tests are actually about.
    """
    def handler(request):
        key = (request.method, request.url.path)
        status, body = routes.get(key, default)
        return httpx2.Response(status, json=body)
    return handler


# ------------------------------------------------------------- happy paths --

def test_get_one_link_passes_the_body_through():
    with gateway_with_upstream(router({("GET", f"/links/{LINK['id']}"): (200, LINK)})) as c:
        r = c.get(f"/links/{LINK['id']}")
    assert r.status_code == 200
    assert r.json() == LINK


def test_post_returns_201_and_forwards_location():
    """The Location header is the reason `GET /links/{id}` had to exist here.

    links-service answers 201 with `Location: /links/<id>`. gateway forwards
    it, and because gateway serves that same path the header is a URL the
    caller can actually follow. A gateway that dropped it, or that answered
    502 on the path it points at, would be handing out a broken promise.
    """
    created = {**LINK, "name": "grafana"}

    def handler(request):
        assert request.method == "POST"
        return httpx2.Response(201, json=created,
                               headers={"Location": f"/links/{created['id']}"})

    with gateway_with_upstream(handler) as c:
        r = c.post("/links", json=NEW)

    assert r.status_code == 201
    assert r.json() == created
    assert r.headers["location"] == f"/links/{created['id']}"


def test_post_forwards_the_body_it_was_given():
    """gateway does not own the link schema, so it must not reshape the body.

    links-service defines `LinkCreate`. Copying that model into gateway would
    give the project two definitions of a link, and the day a field is added
    gateway starts stripping it -- silently, because a proxy that drops an
    unknown key looks exactly like a client that never sent one.
    """
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx2.Response(201, json=LINK)

    with gateway_with_upstream(handler) as c:
        c.post("/links", json={**NEW, "icon": "chart", "future_field": "kept"})

    import json
    assert json.loads(seen["body"]) == {**NEW, "icon": "chart", "future_field": "kept"}


def test_delete_passes_the_upstream_body_through():
    with gateway_with_upstream(
        router({("DELETE", f"/links/{LINK['id']}"): (200, {"deleted": LINK["id"]})})
    ) as c:
        r = c.delete(f"/links/{LINK['id']}")
    assert r.status_code == 200
    assert r.json() == {"deleted": LINK["id"]}


# ------------------------------------------------------------- passthrough --

@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_unknown_id_is_404_not_502(method):
    """The central test in this file.

    A 404 for an id that does not exist is a correct answer, not an upstream
    fault. Mapping it to 502 would tell the caller the server is broken when
    in fact they asked for something that is not there -- and it would send
    them to read gateway's logs instead of checking their id.
    """
    with gateway_with_upstream(router({})) as c:
        r = c.request(method, "/links/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "Link not found"}


def test_invalid_body_is_422_not_502():
    """Same rule from the other direction: 422 is about what the CALLER sent."""
    validation = {"detail": [{"loc": ["body", "url"], "msg": "Field required",
                              "type": "missing"}]}
    with gateway_with_upstream(responds(422, validation)) as c:
        r = c.post("/links", json={"name": "no url"})
    assert r.status_code == 422
    assert r.json() == validation


def test_list_still_maps_404_to_502():
    """`GET /links` has NO passthrough, and that asymmetry is deliberate.

    The collection always exists. A 404 there is not "no such link", it is
    links-service serving something other than what gateway thinks it is --
    a wrong URL, a stray proxy, a rolled-back deploy. 502 is the honest answer
    and is what makes the 404s above meaningful rather than ambient.
    """
    with gateway_with_upstream(responds(404, {"detail": "Not Found"})) as c:
        r = c.get("/links")
    assert r.status_code == 502
    assert r.json() == {"detail": "links-service returned an error"}


def test_passthrough_status_with_a_non_json_body_becomes_502():
    """A 404 in HTML did not come from a links-service handler.

    Something in between rewrote the response -- an ingress, a proxy, an error
    page. That is an upstream fault, so it falls back to 502 rather than
    passing a page of markup off as a link record's 404.
    """
    def handler(request):
        return httpx2.Response(404, text="<html>404</html>",
                               headers={"Content-Type": "text/html"})

    with gateway_with_upstream(handler) as c:
        r = c.get("/links/anything")
    assert r.status_code == 502


# ------------------------------- failure mapping, on every route not just one --

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/links", None),
    ("GET", "/links/abc", None),
    ("POST", "/links", NEW),
    ("DELETE", "/links/abc", None),
])
@pytest.mark.parametrize("failure,expected", [
    (raises(httpx2.ConnectError("refused")), 503),
    (raises(httpx2.ReadTimeout("too slow")), 504),
    (responds(500, {"detail": "boom"}), 502),
])
def test_every_route_maps_upstream_failure_the_same_way(method, path, body, failure, expected):
    """The regression guard for factoring the try/except into `_proxy`.

    Four routes sharing one helper is only an improvement while they really do
    share it. If a future route grows its own copy of the error handling, or
    forgets `passthrough`, this matrix is what notices -- twelve cases for the
    cost of two parametrize decorators.
    """
    with gateway_with_upstream(failure) as c:
        r = c.request(method, path, json=body)
    assert r.status_code == expected


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/links/abc", None),
    ("POST", "/links", NEW),
    ("DELETE", "/links/abc", None),
])
def test_new_routes_never_leak_the_upstream_address(method, path, body):
    """The step-4 leak fix has to hold on the routes added after it.

    A fix applied to one handler and not to the three written later is the
    normal way a security fix decays.
    """
    # The same leak list as `test_gateway.py`, deliberately. The bare name
    # "links-service" is NOT a leak -- it is the whole point of the message,
    # and it is what tells the reader which component to go and look at. What
    # must not escape is the ADDRESS: scheme, host and port.
    with gateway_with_upstream(raises(httpx2.ConnectError("boom"))) as c:
        r = c.request(method, path, json=body)
    assert r.status_code == 503
    for leak in ("localhost", "links-service:8000", "links-service:80",
                 "http://", "8000"):
        assert leak not in r.text
