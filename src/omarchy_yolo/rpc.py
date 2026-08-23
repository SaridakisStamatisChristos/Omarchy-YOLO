from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from .util import YoloError


class RpcError(YoloError):
    pass


async def rpc_call(socket_path: Path, method: str, params: dict[str, Any] | None = None) -> Any:
    request_id = uuid.uuid4().hex
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RpcError(f"daemon unavailable at {socket_path}") from exc
    try:
        payload = {"id": request_id, "method": method, "params": params or {}}
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=30)
        if not raw:
            raise RpcError("daemon closed RPC connection without a response")
        response = json.loads(raw)
        if response.get("id") != request_id:
            raise RpcError("daemon returned a mismatched RPC response")
        if not response.get("ok"):
            raise RpcError(str(response.get("error", "unknown daemon error")))
        return response.get("result")
    finally:
        writer.close()
        await writer.wait_closed()
