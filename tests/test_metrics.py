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
from test_gateway import gateway_with_upstream, reload_main, responds


def _counter(body: str, handler: str) -> float:
    """The value of http_requests_total for one handler, or 0.0 if absent.

    Absent and zero are the same thing for a counter that has never been
    incremented, so collapsing them is safe HERE -- unlike most places in this
    project, where "I cannot see it" and "it is not there" must stay distinct.
    """
    prefix = f'http_requests_total{{handler="{handler}"'
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


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


def test_metrics_still_record_after_a_module_reload():
    """Reloading app.main must not leave instrumentation inert.

    This is the direct guard for the defect found on 2026-09-16, and it exists
    because the defect was previously caught only by ACCIDENT -- the test above
    happened to sort after the two reload tests in test_gateway.py, so it went
    red for a reason that had nothing to do with label cardinality.

    What goes wrong without `reload_main`'s cleanup: reloading re-registers
    collector names that are already in prometheus_client's process-wide
    REGISTRY, the duplicate is swallowed rather than raised, and the reloaded
    app then records NOTHING. The old collectors survive holding their old
    values, so /metrics keeps serving a plausible body frozen at the reload.

    That is the failure mode this project keeps meeting in other clothes: the
    thing does not break loudly, it goes quiet and keeps answering.

    THE REQUEST BEFORE THE RELOAD IS LOAD-BEARING, and the first version of
    this test did not have it and therefore passed against the bug.

    A reload on its own is harmless. The damage needs a labelled child series
    to already exist -- measured both ways in separate processes:

        3 requests, THEN reload, then 5 more  ->  3.0   (the 5 vanish)
        reload FIRST, then 5 requests         ->  5.0   (fine)

    So the guard has to dirty the registry first, exactly as a real suite does
    by the time it reaches the reload tests. A regression test that exercises
    the one ordering where the bug does not bite is not a guard, it is
    decoration -- which is what made this worth re-checking rather than
    trusting a green tick.

    Counting exactly two rather than "> 0" is also deliberate. `reload_main`
    unregisters the collectors, so the counter restarts at zero and only
    working instrumentation reaches 2.0: an inert one keeps whatever the
    pre-reload request left behind.
    """
    with TestClient(main.app) as client:
        client.get("/health")

    reload_main()

    with TestClient(main.app) as client:
        client.get("/health")
        client.get("/health")
        body = client.get("/metrics").text

    assert _counter(body, "/health") == 2.0, (
        "instrumentation is dead after reload -- see reload_main() in test_gateway.py"
    )


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
