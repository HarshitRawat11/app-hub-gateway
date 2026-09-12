from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import httpx2
import logging
import os

logger = logging.getLogger(__name__)

# Where links-service lives. This is config, not code: "http://localhost:8000"
# on a laptop, "http://links-service:80" inside the app-hub namespace. The
# Deployment's env: block supplies the second one, so the same image runs in
# both places without a rebuild.
#
# Note the port changes as well as the host, which is easy to miss: 8000 is
# the port this process and links-service LISTEN on, but the links-service
# Service exposes 80 and forwards to 8000. A consumer addresses the Service.
#
# rstrip("/") because a trailing slash in the variable produces "...8000//links",
# which some servers tolerate and some 404 on -- a miserable bug to read, since
# the URL looks correct.
LINKS_SERVICE_URL = os.getenv("LINKS_SERVICE_URL", "http://localhost:8000").rstrip("/")

# The dashboard (S-03). Resolved from __file__ rather than the process's
# working directory, because uvicorn can be started from anywhere and a
# relative path would work in dev and 404 in the container.
STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx2.AsyncClient(timeout=3.0)
    yield
    await app.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    # Deliberately does NOT check links-service. This backs the liveness probe,
    # so a failing upstream would get gateway killed and restarted as well --
    # one outage becoming two, with the restarts hiding the real cause.
    return {"status": "ok"}


# ---------------------------------------------------------------- the proxy --

async def _proxy(method: str, path: str, json_body=None, passthrough=frozenset()):
    """Call links-service and map its failures onto gateway's own.

    The exception handling here is unchanged from the owner-written version in
    `get_links` -- same order, same fixed detail strings. It was moved into a
    helper rather than rewritten, because four routes copy-pasting one
    try/except is how one of them ends up subtly different.

    `passthrough` names the upstream statuses that are the CLIENT's answer
    rather than a gateway fault. That distinction is the whole reason this
    takes an argument:

      - `GET /links` -- the collection always exists, so a 404 means something
        is wrong upstream. 502 is right.
      - `GET /links/<unknown>` -- a 404 is the correct, useful answer. Turning
        it into 502 would tell the caller "the server is broken" when in fact
        they asked for something that is not there.
      - `POST /links` with a bad body -- 422 is about what the CALLER sent.
        Reporting that as 502 sends them looking at the wrong machine.
    """
    url = f"{LINKS_SERVICE_URL}{path}"
    try:
        response = await app.state.http_client.request(method, url, json=json_body)
    except httpx2.TimeoutException:
        # The detail strings stay fixed. str(e) from httpx2 contains the URL it
        # tried, which in-cluster is "http://links-service:80/links" -- that
        # is internal topology, and the caller has no business seeing it. The
        # real error goes to the logs, where it is actually useful.
        logger.warning("timeout after 3s calling %s %s", method, url)
        raise HTTPException(status_code=504, detail="links-service timed out")
    except httpx2.RequestError as e:
        logger.warning("cannot reach %s %s: %s", method, url, e)
        raise HTTPException(status_code=503, detail="links-service unavailable")

    if response.status_code in passthrough:
        # Forward the upstream body for these, unlike the generated errors
        # above. It is safe for a different reason than it looks: the 503/504
        # details come from httpx2 exception text, which embeds the URL that
        # was attempted. A passthrough body comes from a links-service HANDLER
        # -- "Link not found", or a pydantic report of the caller's own
        # fields -- and carries no topology at all.
        try:
            body = response.json()
        except ValueError:
            # A declared-passthrough status arriving as non-JSON means
            # something between here and links-service rewrote the response.
            # That is an upstream fault, not the caller's answer.
            logger.warning("%s %s returned non-JSON HTTP %s",
                           method, url, response.status_code)
            raise HTTPException(status_code=502,
                                detail="links-service returned an error")
        detail = body.get("detail", body) if isinstance(body, dict) else body
        raise HTTPException(status_code=response.status_code, detail=detail)

    if response.status_code >= 400:
        logger.warning("%s %s returned HTTP %s", method, url, response.status_code)
        raise HTTPException(status_code=502, detail="links-service returned an error")

    return response


@app.get("/links")
async def get_links():
    response = await _proxy("GET", "/links")
    return response.json()


@app.get("/links/{link_id}")
async def get_link(link_id: str):
    # 404 passes through: the Location header POST hands back points here, so
    # a URL gateway answers with 502 would make that header a lie.
    response = await _proxy("GET", f"/links/{link_id}", passthrough={404})
    return response.json()


@app.post("/links", status_code=201)
async def create_link(response: Response, payload: dict = Body(...)):
    # `dict`, not a LinkCreate copied over from links-service. links-service
    # owns that schema; duplicating it here would give the project two
    # definitions of a link that drift apart silently, and gateway would start
    # rejecting fields it had simply not been told about yet. Validation stays
    # where the data lives, and its 422 comes back through `passthrough`.
    upstream = await _proxy("POST", "/links", json_body=payload, passthrough={422})
    # Forward Location. Copied deliberately rather than passing headers through
    # wholesale: it is only correct because gateway happens to serve the same
    # path, and this is where that assumption should break if it ever stops
    # being true.
    if "Location" in upstream.headers:
        response.headers["Location"] = upstream.headers["Location"]
    return upstream.json()


@app.delete("/links/{link_id}")
async def delete_link(link_id: str):
    response = await _proxy("DELETE", f"/links/{link_id}", passthrough={404})
    return response.json()


# ------------------------------------------------------------ the dashboard --
#
# S-03. The dashboard is served BY gateway rather than by a service of its own.
# Reasoning in learn/27; the short version is that same-origin means no CORS,
# no second image, and no second load balancer on a cluster destroyed nightly.
#
# Mounted at /static with an explicit route for "/", rather than mounting
# StaticFiles at "/" with html=True. A mount at "/" matches every path, so it
# works only while it is the LAST route registered -- and the day someone adds
# a route below it, that route silently serves index.html instead. Being
# explicit is worth three lines here.

class RevalidatingStatic(StaticFiles):
    """StaticFiles, but the browser must check before reusing a cached copy.

    The asset filenames are stable across deploys -- there is no content hash
    in `style.css`. So a browser that decides a cached copy is still fresh
    serves the OLD stylesheet against the NEW page, with no error anywhere:
    the page renders, just wrong. That happened during S-03's own verification
    and cost a confused ten minutes.

    `no-cache` does not mean "do not cache". It means "cache, but revalidate
    every time" -- so the browser still sends `If-None-Match`, StaticFiles
    still answers `304 Not Modified`, and almost nothing goes over the wire.
    For three small files on a personal dashboard that is the right trade
    outright; a big asset bundle would want content-hashed filenames instead.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/", include_in_schema=False)
def dashboard():
    # Same reasoning as above: the page that names the assets must not itself
    # be served from a stale cache, or the versions can never get back in step.
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", RevalidatingStatic(directory=STATIC_DIR), name="static")
