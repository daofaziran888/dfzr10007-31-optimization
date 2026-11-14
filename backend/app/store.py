"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._entry_locks: dict[tuple[str, int], threading.RLock] = {}
        # 幂等结果缓存：同一动作携带同一幂等键重复提交时，返回首次结果而不是重复执行。
        self._idempotent: dict[tuple[str, str], dict[str, Any]] = {}

    def lock(self) -> threading.RLock:
        """全局读写锁；内存仓库的增删改统一走这里，避免并发下重复落记录。"""
        return self._lock

    def entry_lock(self, module: str, entry_id: int) -> threading.RLock:
        """单条记录粒度的锁，保证同一规则的触发 / 流转不会交叉写入。"""
        with self._lock:
            key = (module, int(entry_id))
            return self._entry_locks.setdefault(key, threading.RLock())

    def idempotent_result(self, module: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        with self._lock:
            cached = self._idempotent.get((module, key))
            return dict(cached) if cached is not None else None

    def remember_idempotent(self, module: str, key: str | None, entry: Any, message: str) -> None:
        if not key:
            return
        with self._lock:
            self._idempotent[(module, key)] = {"entry": entry, "message": message}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
