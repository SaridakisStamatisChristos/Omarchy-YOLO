from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any

from .util import YoloError


MAX_RPC_MESSAGE_BYTES = 1_048_576


class RpcError(YoloError):
    pass


class RpcUnavailable(RpcError):
    pass


class RpcRemoteError(RpcError):
    pass


async def rpc_call(socket_path: Path, method: str, params: dict[str, Any] | None = None) -> Any:
    request_id = uuid.uuid4().hex
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(socket_path), limit=MAX_RPC_MESSAGE_BYTES + 1
        )
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RpcUnavailable(f"daemon unavailable at {socket_path}") from exc
    try:
        payload = {"id": request_id, "method": method, "params": params or {}}
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RPC_MESSAGE_BYTES:
            raise RpcError("RPC request exceeds the maximum message size")
        writer.write(encoded)
        await asyncio.wait_for(writer.drain(), timeout=30)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
        except ValueError as exc:
            raise RpcError("daemon RPC response exceeded the maximum message size") from exc
        if not raw:
            raise RpcUnavailable("daemon closed RPC connection without a response")
        if len(raw) > MAX_RPC_MESSAGE_BYTES:
            raise RpcError("daemon RPC response exceeded the maximum message size")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RpcError("daemon returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RpcError("daemon returned an invalid RPC envelope")
        if response.get("id") != request_id:
            raise RpcError("daemon returned a mismatched RPC response")
        if not response.get("ok"):
            raise RpcRemoteError(str(response.get("error", "unknown daemon error")))
        return response.get("result")
    finally:
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=5)
