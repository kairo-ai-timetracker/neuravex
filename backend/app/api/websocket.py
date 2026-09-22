from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/dashboard/{account_id}")
async def dashboard_ws(websocket: WebSocket, account_id: str):
    await websocket.accept()
    try:
        while True:
            # Placeholder heartbeat — a future version can push real
            # updates here instead of the dashboard polling on a timer.
            await asyncio.sleep(15)
            await websocket.send_json({"type": "heartbeat", "account_id": account_id})
    except WebSocketDisconnect:
        pass
