"""The dashboard gateway serves (S-03).

These are server-side tests: that the files are reachable, that adding them
did not shadow the API, and that the page is wired to the paths it thinks it
is. The browser behaviour -- filtering, the add form, the delete confirm --
is NOT covered here, and saying so plainly beats leaving a reader to assume
otherwise.
"""

import re

from fastapi.testclient import TestClient

from app import main
from app.main import STATIC_DIR


def client():
    """`main.app` resolved per call, never an `app` imported at module scope.

    The config tests in `test_gateway.py` call `importlib.reload(main)`, which
    rebinds `main.app` to a fresh instance while leaving any name already
    imported from the module pointing at the old one. This file happens to
    sort first today. That is not a guarantee.
    """
    return TestClient(main.app)


def test_root_serves_the_dashboard():
    with client() as c:
        r = c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>app-hub</title>" in r.text


def test_static_assets_are_served_with_the_right_types():
    with client() as c:
        css = c.get("/static/style.css")
        js = c.get("/static/app.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_the_api_is_not_shadowed_by_the_static_mount():
    """The reason `/` is an explicit route and not `StaticFiles(html=True)`.

    Mounting StaticFiles at `/` matches every path, so the API keeps working
    only while the mount is the last route registered. Move it up, or add a
    route below it, and `/health` starts returning the dashboard HTML with a
    200 -- which no liveness probe would ever complain about.
    """
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unknown_paths_still_404():
    """Not swallowed into index.html either.

    An SPA would want the opposite -- every unknown path serving the shell so
    client-side routing can take over. This dashboard is one page with no
    routing, so a 404 that says 404 is more useful than a blank page that
    says 200.
    """
    with client() as c:
        assert c.get("/not-a-real-path").status_code == 404
        assert c.get("/static/not-a-real-file.js").status_code == 404


def test_the_page_only_references_assets_that_exist():
    """Catches the rename that leaves a dead `<script src>`.

    A missing stylesheet does not fail loudly in a browser; the page simply
    renders unstyled, and a missing script means nothing happens on load.
    Both look like a bug in the app rather than a 404.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert referenced, "expected the page to reference its own assets"
    with client() as c:
        for path in referenced:
            assert c.get(path).status_code == 200, path


def strip_comments(js):
    """Source with `//` and `/* */` removed.

    Needed because the first version of the innerHTML check below failed on
    app.js's own comment EXPLAINING why innerHTML is not used. A grep over raw
    source cannot tell code from prose, and a check that fires on its own
    documentation gets deleted rather than fixed.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def test_the_dashboard_never_writes_untrusted_data_as_markup():
    """Link records are attacker-controllable; the page renders them as text.

    Anything that can POST to the API decides what `name`, `icon` and `url`
    contain. `textContent` makes that data; `innerHTML` would make it markup.
    A blunt check that holds beats a subtle one nobody runs.
    """
    js = strip_comments((STATIC_DIR / "app.js").read_text(encoding="utf-8"))
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, f"{sink} used on data that came from the API"


def test_link_urls_are_scheme_checked_before_reaching_an_href():
    """`textContent` does not protect an `href`.

    A stored `javascript:...` URL is inert as text and executes on click as an
    href, with this page's origin. So the XSS guard above is necessary and not
    sufficient, and this is the other half of it.
    """
    js = strip_comments((STATIC_DIR / "app.js").read_text(encoding="utf-8"))
    assert "safeHref" in js
    assert re.search(r'\bhttps?:', js), "expected an explicit scheme allowlist"
    assert re.search(r"a\.href\s*=\s*href", js), \
        "href must be assigned only from the checked value"


def test_the_dashboard_calls_the_api_on_its_own_origin():
    """No absolute URLs to the API in the page's JavaScript.

    Same-origin is the whole reason the dashboard lives inside gateway rather
    than in a service of its own: no CORS headers, and no build-time answer to
    "where is the API?". A hardcoded `http://localhost:8001` would work on a
    laptop and break the moment the page is served from anywhere else, which
    is the worst possible time to find out.
    """
    js = strip_comments((STATIC_DIR / "app.js").read_text(encoding="utf-8"))
    assert re.search(r"fetch\(", js), "expected the dashboard to call the API"
    assert not re.search(r'fetch\(\s*["\'`]https?://', js)
