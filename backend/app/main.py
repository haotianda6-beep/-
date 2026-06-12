import asyncio
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import engine, router

logger = logging.getLogger(__name__)
app = FastAPI(title="Perpetual Spread Arbitrage Bot", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/credentials"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/realtime")
async def realtime_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            try:
                payload = await asyncio.to_thread(lambda: engine.snapshot().model_dump_json())
            except Exception as exc:  # noqa: BLE001 - keep realtime connection alive through transient exchange issues.
                logger.warning("realtime snapshot failed: %s", str(exc)[:200])
                await asyncio.sleep(1.0)
                continue
            try:
                await websocket.send_text(payload)
            except RuntimeError as exc:
                logger.info("realtime socket closed: %s", str(exc)[:160])
                return
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
