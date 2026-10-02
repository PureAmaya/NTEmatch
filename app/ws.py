"""WebSocket 广播中心。

任何配置变更都会把完整状态推给所有在线客户端，
前端据此自动刷新，无需手动轮询。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import WebSocket

from .logging_conf import get_logger

log = get_logger("ws")

MAX_CLIENTS = 500


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._last_state: dict[str, Any] | None = None
        self._broadcast_count = 0

    @property
    def size(self) -> int:
        return len(self._clients)

    @property
    def has_state(self) -> bool:
        """是否已有可回放的最近状态。"""
        return self._last_state is not None

    async def connect(self, ws: WebSocket) -> bool:
        await ws.accept()
        async with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                log.warning("连接数达到上限 %d，拒绝新连接", MAX_CLIENTS)
                await ws.close(code=1013, reason="server busy")
                return False
            self._clients.add(ws)
            total = len(self._clients)
        log.info("客户端接入 | 在线=%d", total)
        if self._last_state is not None:
            await self._send(ws, {"type": "state", "data": self._last_state})
        return True

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            total = len(self._clients)
        log.info("客户端断开 | 在线=%d", total)

    async def broadcast_state(self, state: dict[str, Any]) -> None:
        self._last_state = state
        await self.broadcast({"type": "state", "data": state})

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        results = await asyncio.gather(
            *(self._safe_send(ws, body) for ws in targets), return_exceptions=True
        )
        dead = [ws for ws, res in zip(targets, results) if res is False]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
            log.info("清理失效连接 %d 个 | 在线=%d", len(dead), len(self._clients))
        self._broadcast_count += 1

    async def send_state(self, ws: WebSocket, state: dict[str, Any]) -> bool:
        """向单个客户端推送完整状态（用于连接握手）。"""
        return await self._send(ws, {"type": "state", "data": state})

    async def _send(self, ws: WebSocket, payload: dict[str, Any]) -> bool:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _safe_send(self, ws: WebSocket, body: str) -> bool:
        try:
            await ws.send_text(body)
            return True
        except Exception:  # noqa: BLE001
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "online": len(self._clients),
            "broadcasts": self._broadcast_count,
            "ts": time.time(),
        }


hub = Hub()
