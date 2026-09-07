from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import httpx

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=3.0)
    yield
    await app.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    return {"status":'ok'}

@app.get("/links")
async def getLinks():
    try:
        response = await app.state.http_client.get("http://localhost:8000/links")
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail=str(e))
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="links-service returned an error")

    return response.json()

