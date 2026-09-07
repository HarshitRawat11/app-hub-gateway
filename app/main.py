from fastapi import FastAPI
import httpx

app = FastAPI()

@app.get("/health")
def health():
    return {"status":'ok'}

@app.get("/links")
async def getLinks():
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8000/links")
        return response.json()

