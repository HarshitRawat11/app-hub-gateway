from fastapi import FastAPI
from contextlib import asynccontextmanager
import httpx

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient()
    print("client created")
    yield
    await app.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    return {"status":'ok'}

@app.get("/links")
async def getLinks():
    response = await app.state.http_client.get("http://localhost:8000/links")
    return response.json()
