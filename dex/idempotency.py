"""幂等键存储：相同 key + 相同请求内容 -> 复用原结果；内容不一致 -> 拒绝。

使用方式：副作用发生前先 lookup（命中则直接复用/拒绝），
副作用成功完成后再 record。失败的请求不占用幂等键。
"""
from __future__ import annotations

import hashlib
import json
import logging

from .errors import IdempotencyConflict

logger = logging.getLogger("dex.idempotency")


def fingerprint(payload: dict) -> str:
    """对请求内容做规范化指纹（键序无关）。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self) -> None:
        # key -> {"fingerprint": str, "result": dict}
        self._records: dict[str, dict] = {}

    def lookup(self, key: str, fp: str) -> dict | None:
        """返回已记录的结果；未记录返回 None；内容冲突抛 IdempotencyConflict。"""
        record = self._records.get(key)
        if record is None:
            return None
        if record["fingerprint"] != fp:
            logger.warning("idempotency conflict key=%s", key)
            raise IdempotencyConflict(
                f"idempotency key {key!r} reused with different request content")
        logger.info("idempotency replay key=%s", key)
        return record["result"]

    def record(self, key: str, fp: str, result: dict) -> None:
        self._records[key] = {"fingerprint": fp, "result": result}
        logger.info("idempotency record key=%s stored", key)

    def snapshot(self) -> dict:
        return self._records

    def restore(self, data: dict) -> None:
        self._records = dict(data)
