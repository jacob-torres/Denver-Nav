"""Start a minimal FastAPI app and confirm whether httpx hangs inside it."""
import asyncio, threading, httpx
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/ping")
async def ping():
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get("https://www.google.com")
        return {"status": r.status_code}

def run_server():
    uvicorn.run(app, port=8002, log_level="error")

t = threading.Thread(target=run_server, daemon=True)
t.start()
import time; time.sleep(2)

async def test():
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get("http://localhost:8002/ping")
            print("Result:", r.json())
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")

asyncio.run(test())
