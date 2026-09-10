from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import httpx
import logging
import os

logger = logging.getLogger(__name__)

# Where links-service lives. This is config, not code: "http://localhost:8000"
# on a laptop, "http://links-service:8000" inside the app-hub namespace. The
# Deployment's env: block supplies the second one, so the same image runs in
# both places without a rebuild.
#
# rstrip("/") because a trailing slash in the variable produces "...8000//links",
# which some servers tolerate and some 404 on -- a miserable bug to read, since
# the URL looks correct.
LINKS_SERVICE_URL = os.getenv("LINKS_SERVICE_URL", "http://localhost:8000").rstrip("/")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=3.0)
    yield
    await app.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    # Deliberately does NOT check links-service. This backs the liveness probe,
    # so a failing upstream would get gateway killed and restarted as well --
    # one outage becoming two, with the restarts hiding the real cause.
    return {"status": "ok"}

@app.get("/links")
async def get_links():
    url = f"{LINKS_SERVICE_URL}/links"
    try:
        response = await app.state.http_client.get(url)
    except httpx.TimeoutException:
        # The detail strings stay fixed. str(e) from httpx contains the URL it
        # tried, which in-cluster is "http://links-service:8000/links" -- that
        # is internal topology, and the caller has no business seeing it. The
        # real error goes to the logs, where it is actually useful.
        logger.warning("timeout after 3s calling %s", url)
        raise HTTPException(status_code=504, detail="links-service timed out")
    except httpx.RequestError as e:
        logger.warning("cannot reach %s: %s", url, e)
        raise HTTPException(status_code=503, detail="links-service unavailable")
    if response.status_code >= 400:
        logger.warning("%s returned HTTP %s", url, response.status_code)
        raise HTTPException(status_code=502, detail="links-service returned an error")

    return response.json()
