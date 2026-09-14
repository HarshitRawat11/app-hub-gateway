"""The /metrics endpoint on gateway.

The cardinality reasoning is spelled out in links-service/tests/test_metrics.py
and applies identically here: gateway has `/links/{link_id}` routes, so the raw
id must never reach a label.

One thing is gateway-specific and worth pinning: gateway mounts StaticFiles at
/static and serves the dashboard at "/". Adding /metrics after those must not
be shadowed by either, and the dashboard must not be shadowed by it.
"""

import uuid

import httpx2
from fastapi.testclient import TestClient

from app import main
from test_gateway import gateway_with_upstream, responds


def test_metrics_endpoint_is_served_and_is_prometheus_format():
    with TestClient(main.app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "# HELP" in r.text


def test_metrics_coexists_with_the_dashboard_and_the_static_mount():
    """Route order regression guard.

    /metrics is registered AFTER `app.mount("/static", ...)`. That is safe only
    because the mount is scoped to /static rather than "/" -- a mount at "/"
    would match every path and swallow this. If anyone moves the mount, this
    and test_dashboard.py's shadowing test both fail.
    """
    with TestClient(main.app) as client:
        assert client.get("/metrics").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}


def test_ids_do_not_become_labels():
    unique = str(uuid.uuid4())
    with gateway_with_upstream(responds(404, {"detail": "Link not found"})) as client:
        client.get(f"/links/{unique}")
        body = client.get("/metrics").text
    assert unique not in body, "raw id leaked into a metric label -- unbounded cardinality"
    assert "/links/{link_id}" in body


def test_upstream_failures_are_visible_as_5xx_in_metrics():
    """gateway's whole job is turning upstream failures into honest statuses.

    Those statuses should be countable -- "how often is links-service down?"
    is exactly the question this service exists to answer, and it should be
    answerable from metrics rather than by reading logs.
    """
    with gateway_with_upstream(lambda r: (_ for _ in ()).throw(httpx2.ConnectError("x"))) as client:
        client.get("/links")
        body = client.get("/metrics").text
    assert 'status="5xx"' in body or 'status="503"' in body
