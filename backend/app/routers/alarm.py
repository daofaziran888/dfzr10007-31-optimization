"""分析告警规则接口：规则维护、触发窗口重算、版本快照与监控事件。

路由层只做参数透传与错误翻译，所有判定都收在 ``app.services.alarm``。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alarm import service

router = APIRouter(prefix="/api/alarm", tags=["分析告警规则"])

LIST_FIELDS = [
    "规则编号", "监控指标", "告警条件", "告警级别", "通知渠道",
    "静默周期", "生效范围", "增长阈值", "规则版本", "规则状态",
]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


# ---------------------------------------------------------------- 触发记录
# 静态路径必须声明在 /{entry_id} 之前，否则会被路径参数抢先匹配。
@router.get("/trigger-records", response_model=dict)
def list_trigger_records(
    keyword: str | None = Query(default=None, description="按触发记录号或监控指标检索"),
    rule_code: str | None = Query(default=None, description="按规则编号过滤"),
    metric: str | None = Query(default=None, description="按监控指标过滤"),
    include_history: bool = Query(default=False, description="是否包含已失效的历史触发记录"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """触发记录清单：按监控指标与告警条件归并后，静默窗口内只保留一条。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total, summaries = service.list_trigger_records(
        keyword=keyword,
        rule_code=rule_code,
        metric=metric,
        include_history=include_history,
        page=page,
        size=size,
    )
    # 核对编号随清单一起返回，便于直接核对 ALAR-0002-T2 / ALAR-0003-T0。
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "核对编号": summaries,
    }


@router.get("/trigger-records/export")
def export_trigger_records() -> dict[str, Any]:
    """导出触发记录清单：返回当前过滤条件下的全量数据。"""
    items, total, summaries = service.list_trigger_records(page=1, size=10000)
    return {"module": "alarm", "total": total, "items": items, "核对编号": summaries}


@router.get("/trigger-windows", response_model=dict)
def list_trigger_windows(
    rule_code: str | None = Query(default=None, description="按规则编号过滤"),
    metric: str | None = Query(default=None, description="按监控指标过滤"),
) -> dict[str, Any]:
    """按规则读取当前版本的触发窗口与核对编号。"""
    result = service.recalculate(rule_code=rule_code, metric=metric)
    return result


@router.post("/trigger-windows/recalculate", response_model=dict)
def recalculate_windows(payload: EntryPayload | None = None) -> dict[str, Any]:
    """重算触发窗口：按监控指标与告警条件归并，静默窗口内只落一条。"""
    values = payload.values if payload is not None else {}
    rule_code = str(values.get("规则编号") or "").strip() or None
    metric = str(values.get("监控指标") or "").strip() or None
    return service.recalculate(rule_code=rule_code, metric=metric)


@router.post("/recalculate", response_model=dict)
def recalculate_alias(payload: EntryPayload | None = None) -> dict[str, Any]:
    """触发窗口重算的别名入口，行为与 /trigger-windows/recalculate 一致。"""
    return recalculate_windows(payload)


# ---------------------------------------------------------------- 监控事件
@router.get("/events", response_model=PageResult[dict])
def list_events(
    keyword: str | None = Query(default=None, description="按事件编号检索"),
    metric: str | None = Query(default=None, description="按监控指标过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """监控事件清单：重复事件只保留首次登记的一条。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_events(keyword=keyword, metric=metric, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/events", response_model=ActionResult)
def create_event(payload: EntryPayload) -> ActionResult:
    """登记监控事件；同一事件编号重复提交只生效一次。"""
    entry, message, duplicated = service.create_event(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "重复事件": duplicated})


@router.get("/events/export")
def export_events() -> dict[str, Any]:
    items, total = service.list_events(page=1, size=10000)
    return {"module": "alarm_events", "total": total, "items": items}


# ---------------------------------------------------------------- 版本快照
@router.get("/versions", response_model=PageResult[dict])
def list_versions(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    rule_code: str | None = Query(default=None, description="按规则编号精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """规则版本快照清单：阈值变化后旧条件仍按原值可读。"""
    items, total = service.list_all_versions(keyword=keyword, rule_code=rule_code, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 规则列表
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    metric: str | None = Query(default=None, description="按监控指标过滤"),
    rule_version: int | None = Query(default=None, description="按版本号读取历史条件清单"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤分析告警规则列表；带 rule_version 时按原条件读旧清单。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在允许的状态序列里")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        metric=metric,
        rule_version=rule_version,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析告警规则清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alarm", "total": total, "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


# ---------------------------------------------------------------- 单条规则
@router.get("/{entry_id}/trigger-window", response_model=dict)
def trigger_window(entry_id: int) -> dict[str, Any]:
    """单条规则当前版本的触发窗口：记录号、核对编号与触发依据。"""
    result = service.trigger_window(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return result


@router.post("/{entry_id}/trigger", response_model=dict)
def trigger_rule(entry_id: int) -> dict[str, Any]:
    """按当前条件重算单条规则的触发窗口。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return service.recalculate(rule_code=str(service.get_entry(entry_id)["规则编号"]))


@router.get("/{entry_id}/versions", response_model=list[dict])
def entry_versions(entry_id: int) -> list[dict[str, Any]]:
    """单条规则的版本快照：旧版本按原条件可读。"""
    versions = service.list_versions(entry_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return versions


@router.post("/{entry_id}/versions", response_model=ActionResult)
def add_version(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记规则新版本：阈值变化后新窗口按新条件计算，旧版本按原条件保留。"""
    entry, message = service.add_version(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """按 草稿→已启用→已暂停→已关闭 单向推进；越界配置拒绝进入生效状态。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, idempotency_key=idempotency_key)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
