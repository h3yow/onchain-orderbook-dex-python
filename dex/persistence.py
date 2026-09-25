"""本地持久化：JSON 快照 + 原子写（tmp + rename），支撑服务重启恢复。"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("dex.persistence")


class JsonStore:
    def __init__(self, path: str) -> None:
        self._path = path

    def load(self) -> dict:
        if not os.path.exists(self._path):
            logger.info("no state file at %s, starting fresh", self._path)
            return {}
        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("loaded state from %s", self._path)
        return data

    def save(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)  # 原子替换，避免半写状态
