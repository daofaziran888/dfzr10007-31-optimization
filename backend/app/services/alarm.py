"""分析告警规则业务规则：状态流转、告警条件判定、触发窗口重算与版本快照。

几个口径都收在这里，路由层只做参数透传：

* 触发窗口按「监控指标 + 告警条件」归并；同一静默窗口内只落一条记录，
  记录保留最近一次触发依据（窗口内最后一次触发的事件）；
* 静默窗口 = ``[首个触发事件时间, 首个触发事件时间 + 静默周期)``，
  窗口内重复事件只生效一次，窗口结束后才允许生成下一条；
* 同一监控指标只保留最近触发规则（版本号/更新时间最新的一条），其余同指标
  启用规则不再各自落记录，保证同一指标采用统一判定标准；
* 触发记录号 = ``规则编号-T{触发依据事件在该指标事件序列中的序号}``，
  ``T0`` 表示尚无触发记录（例如规则处于暂停状态）；
* 阈值变化走版本快照：旧告警规则清单按原条件可读，新窗口按新条件计算；
* 重复事件按事件编号去重，重复动作按幂等键返回首次结果。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services import alarm_rules as rules
from app.store import store

MODULE = "alarm"
EVENT_MODULE = "alarm_events"
RECORD_MODULE = "alarm_trigger_records"
HISTORY_MODULE = "alarm_trigger_history"
VERSION_MODULE = "alarm_versions"

REQUIRED_FIELDS = ["规则编号", "监控指标", "告警条件"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
# 动作 -> (允许执行该动作的源状态, 目标状态)；状态只能单向推进。
# 启用同时覆盖草稿首发与暂停后恢复；关闭只允许从已暂停推进。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "启用规则": (("草稿", "已暂停"), "已启用"),
    "暂停规则": (("已启用",), "已暂停"),
    "关闭规则": (("已暂停",), "已关闭"),
}
NEGATIVE_ACTIONS: list[str] = []

DEFAULT_VERSION_TIME = "2026-09-01 00:00:00"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class AlarmService:
    def __init__(self) -> None:
        self._records_ready = False

    # ------------------------------------------------------------ 列表读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        metric: str | None = None,
        rule_version: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """规则清单；带 ``rule_version`` 时按该版本快照的原条件返回旧清单。"""
        self._ensure_records()
        if rule_version is not None:
            rows = self._version_rows(rule_version=rule_version, keyword=keyword, metric=metric)
        else:
            with store.lock():
                rows = [self._rule_view(row) for row in store.rows(MODULE)]
        if keyword and rule_version is None:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if metric:
            rows = [
                row for row in rows
                if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _version_rows(
        self,
        *,
        rule_version: int,
        keyword: str | None = None,
        metric: str | None = None,
    ) -> list[dict[str, Any]]:
        """按版本号取历史快照，转成与当前清单同构的行，便于直接对照原条件。"""
        with store.lock():
            versions = [dict(row) for row in store.rows(VERSION_MODULE) if int(row.get("规则版本", 0)) == rule_version]
            current = {int(row["id"]): row for row in store.rows(MODULE)}
        rows: list[dict[str, Any]] = []
        for version in versions:
            rule = current.get(int(version.get("entry_id", 0)))
            if rule is None:
                continue
            row = dict(rule)
            row["规则版本"] = rule_version
            row["version"] = rule_version
            row["增长阈值"] = version.get("增长阈值")
            row["静默周期"] = version.get("静默周期")
            row["静默周期_秒"] = version.get("静默周期_秒")
            row["生效范围"] = version.get("生效范围")
            row["历史版本"] = True
            row["变更说明"] = version.get("变更说明")
            row["触发记录数"] = 0
            row["核对编号"] = f"{row.get('规则编号')}-T0"
            row["告警触发结果"] = "未触发"
            rows.append(row)
        if rule_version is not None:
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
            if metric:
                rows = [
                    row for row in rows
                    if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)
                ]
        rows.sort(key=lambda row: str(row.get("规则编号", "")))
        return rows

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_records()
        with store.lock():
            row = store.find(MODULE, entry_id)
            return None if row is None else self._rule_view(row)

    # ------------------------------------------------------------ 规则登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"

        rule_code = _text(values.get("规则编号"))
        condition = _text(values.get("告警条件"))
        metric = _text(values.get("监控指标"))

        # 阈值与静默周期统一走判定标准；登记阶段允许静默周期先存草稿（启用时校验上限），
        # 但告警条件必须能解析出指标与阈值，否则不允许登记，避免落一条无法判定的规则。
        condition_parsed = rules.parse_condition(condition)
        if condition_parsed is None:
            return None, "告警条件无法解析，需形如「订单GMV 同环比增长率 >= 15%」，不允许登记"
        raw_threshold = rules.pick(values, "增长阈值", "growth_threshold")
        if raw_threshold is None:
            raw_threshold = rules.threshold_text(condition_parsed["threshold_percent"])
        try:
            percent, _ratio = rules.parse_growth_threshold(raw_threshold, enforce_limit=False)
        except rules.StandardError as exc:
            return None, str(exc)
        raw_silence = rules.pick(values, "静默周期", "静默周期_秒", "silence", "silence_seconds")
        raw_silence = rules.DEFAULT_SILENCE_SECONDS if raw_silence is None else raw_silence
        try:
            silence = rules.parse_silence(raw_silence, enforce_limit=False)
        except rules.StandardError as exc:
            return None, str(exc)
        raw_scope = rules.pick(values, "生效范围", "生效时段", "effective_scope")
        try:
            scope = rules.parse_scope(raw_scope)
            scope_text = scope.normalized
        except rules.StandardError:
            # 草稿允许先存下来，启用时统一拦下；但空配置按全天处理。
            scope_text = _text(raw_scope) or rules.DEFAULT_SCOPE
            scope = None

        with store.lock():
            if any(_text(row.get("规则编号")) == rule_code for row in store.rows(MODULE)):
                return None, f"规则编号「{rule_code}」已存在，重复登记只保留一份"
            rows = store.rows(MODULE)
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "version": 1,
                "updated_at": rules.format_datetime(datetime.now()),
            }
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["监控指标"] = metric
            entry["告警条件"] = condition
            entry["告警级别"] = values.get("告警级别")
            entry["通知渠道"] = values.get("通知渠道")
            entry["增长阈值"] = rules.threshold_text(percent)
            entry["静默周期"] = rules.silence_text(silence)
            entry["静默周期_秒"] = silence
            entry["生效范围"] = scope_text
            entry["生效时段"] = scope_text
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            self._snapshot_version(entry, reason="初始版本")
            return self._rule_view(entry), "告警规则已登记"

    # ------------------------------------------------------------ 状态流转
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        scoped_key = f"{entry_id}:{action}:{idempotency_key}" if idempotency_key else None
        cached = store.idempotent_result(MODULE, scoped_key)
        if cached is not None:
            return cached.get("entry"), str(cached.get("message") or "")

        lock = store.entry_lock(MODULE, entry_id)
        lock.acquire()
        try:
            self._ensure_records()
            with store.lock():
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"告警规则 {entry_id} 不存在或已归档"
                current = _text(entry.get("status"))
                if action not in ACTION_RULES:
                    return None, f"动作「{action}」不属于分析告警规则可执行范围"

                allowed_sources, target = ACTION_RULES[action]
                if current == target:
                    view = self._rule_view(entry)
                    store.remember_idempotent(MODULE, scoped_key, view, "重复动作不重复生效，返回首次结果")
                    return view, f"告警规则已是{target}，重复动作不重复生效"
                if current not in allowed_sources:
                    return None, (
                        f"告警规则当前为{current}，{action}只能从"
                        f"{'、'.join(allowed_sources)}推进到{target}，记录保持不变"
                    )

                if action == "启用规则":
                    error = self._activation_error(entry)
                    if error:
                        return None, f"拒绝进入生效状态：{error}"

                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = target == "已启用"
                entry["updated_at"] = rules.format_datetime(datetime.now())
                self._rebuild_records()
                view = self._rule_view(entry)
                message = f"告警规则已{action}"
                store.remember_idempotent(MODULE, scoped_key, view, message)
                return view, message
        finally:
            lock.release()

    def _activation_error(self, entry: dict[str, Any]) -> str | None:
        """进入生效状态前的统一校验：监控条件、静默阈值、生效范围。"""
        if not _text(entry.get("监控指标")):
            return "缺少监控指标，无法判定触发"
        if rules.parse_condition(entry.get("告警条件")) is None:
            return "告警条件无法解析，需形如「订单GMV 同环比增长率 >= 15%」"
        percent = self._percent_of(entry, enforce=True)
        if percent is None:
            return "增长阈值无法解析"
        silence = self._silence_of(entry, enforce=True)
        if silence is None:
            return "静默周期无法解析或超出上限"
        if self._scope_of(entry) is None:
            return "生效范围无法解析"
        return None

    # ------------------------------------------------------------ 条件变更版本
    def add_version(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记规则的新版本：旧版本按原条件保留可读，新窗口按新条件计算。"""
        lock = store.entry_lock(MODULE, entry_id)
        lock.acquire()
        try:
            self._ensure_records()
            with store.lock():
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"告警规则 {entry_id} 不存在或已归档"

                active = _text(entry.get("status")) == "已启用"
                metric = _text(rules.pick(values, "监控指标", default=entry.get("监控指标")))
                condition = _text(rules.pick(values, "告警条件", default=entry.get("告警条件")))
                raw_threshold = rules.pick(values, "增长阈值", "growth_threshold")
                if raw_threshold is None:
                    parsed = rules.parse_condition(condition)
                    if parsed is None:
                        return None, "告警条件无法解析，需形如「订单GMV 同环比增长率 >= 15%」"
                    percent = parsed["threshold_percent"]
                else:
                    try:
                        percent, _ratio = rules.parse_growth_threshold(raw_threshold, enforce_limit=active)
                    except rules.StandardError as exc:
                        return None, str(exc)
                    condition = rules.rebuild_condition(condition, percent)

                raw_silence = rules.pick(values, "静默周期", "静默周期_秒", "silence", "silence_seconds")
                try:
                    silence = (
                        self._silence_of(entry, enforce=active)
                        if raw_silence is None
                        else rules.parse_silence(raw_silence)
                    )
                except rules.StandardError as exc:
                    return None, str(exc)
                if silence is None:
                    return None, "静默周期无法解析或超出上限"

                raw_scope = rules.pick(values, "生效范围", "生效时段", "effective_scope")
                try:
                    scope = self._scope_of(entry) if raw_scope is None else rules.parse_scope(raw_scope)
                except rules.StandardError as exc:
                    return None, str(exc)
                if scope is None:
                    return None, "生效范围无法解析"

                unchanged = (
                    metric == _text(entry.get("监控指标"))
                    and condition == _text(entry.get("告警条件"))
                    and percent == self._percent_of(entry, enforce=False)
                    and silence == self._silence_of(entry)
                    and scope.normalized == rules.scope_label(self._scope_of(entry))
                )
                if unchanged:
                    view = self._rule_view(entry)
                    return view, "配置未变化，重复变更不重复生效，沿用当前版本"

                entry["监控指标"] = metric
                entry["告警条件"] = condition
                entry["增长阈值"] = rules.threshold_text(percent)
                entry["静默周期"] = rules.silence_text(silence)
                entry["静默周期_秒"] = silence
                entry["生效范围"] = scope.normalized
                entry["生效时段"] = scope.normalized
                entry["version"] = int(entry.get("version", 1)) + 1
                entry["updated_at"] = rules.format_datetime(datetime.now())
                reason = _text(values.get("变更说明")) or "告警条件变更"
                self._snapshot_version(entry, reason=reason)
                self._rebuild_records()
                view = self._rule_view(entry)
                return view, f"告警规则已登记版本 {entry['version']}，新触发窗口按新条件计算"
        finally:
            lock.release()

    def list_versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        """读取某条规则的全部版本快照：旧版本按原条件可读。"""
        with store.lock():
            if store.find(MODULE, entry_id) is None:
                return None
            return [
                dict(row)
                for row in store.rows(VERSION_MODULE)
                if int(row.get("entry_id", 0)) == entry_id
            ]

    def list_all_versions(
        self,
        *,
        keyword: str | None = None,
        rule_code: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock():
            rows = [dict(row) for row in store.rows(VERSION_MODULE)]
        if rule_code:
            rows = [row for row in rows if _text(row.get("规则编号")) == rule_code]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _snapshot_version(self, entry: dict[str, Any], *, reason: str) -> None:
        versions = store.rows(VERSION_MODULE)
        for row in versions:
            if int(row.get("entry_id", 0)) == int(entry["id"]):
                row["是否当前"] = False
        versions.append({
            "id": max((int(row.get("id", 0)) for row in versions), default=0) + 1,
            "entry_id": int(entry["id"]),
            "规则编号": entry.get("规则编号"),
            "规则版本": int(entry.get("version", 1)),
            "监控指标": entry.get("监控指标"),
            "告警条件": entry.get("告警条件"),
            "增长阈值": entry.get("增长阈值"),
            "增长阈值_数值": self._percent_of(entry, enforce=False),
            "静默周期": entry.get("静默周期"),
            "静默周期_秒": self._silence_of(entry),
            "生效范围": entry.get("生效范围"),
            "生效时间": entry.get("updated_at"),
            "变更说明": reason,
            "是否当前": True,
        })

    # ------------------------------------------------------------ 监控事件
    def create_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """登记监控事件；同一事件编号重复提交只生效一次。"""
        metric = _text(rules.pick(values, "监控指标", "metric"))
        if not metric:
            return None, "缺少必填字段：监控指标", False
        raw_growth = rules.pick(values, "实际增长", "异动幅度", "growth", "growth_rate")
        if raw_growth is None:
            return None, "缺少必填字段：实际增长", False
        growth = rules.growth_value(raw_growth)
        if growth is None:
            return None, f"实际增长「{raw_growth}」无法解析为数值", False

        event_code = _text(rules.pick(values, "事件编号", "event_id"))
        moment = rules.parse_datetime(
            rules.pick(values, "事件时间", "异动时间", "event_time"),
            default=datetime.now(),
        )

        with store.lock():
            events = store.rows(EVENT_MODULE)
            if not event_code:
                event_code = f"EVT-{max((int(row.get('id', 0)) for row in events), default=0) + 1:04d}"
            for row in events:
                if _text(row.get("事件编号")) == event_code:
                    return dict(row), f"事件「{event_code}」重复提交，只生效一次，返回首次登记结果", True
                if (
                    rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)
                    and rules.parse_datetime(row.get("事件时间"), default=moment) == moment
                    and rules.growth_value(row.get("实际增长")) == growth
                ):
                    return dict(row), "同一指标同一时刻同样幅度的事件已登记，重复事件不重复生效", True
            event = {
                "id": max((int(row.get("id", 0)) for row in events), default=0) + 1,
                "事件编号": event_code,
                "监控指标": metric,
                "实际增长": f"{growth:g}%",
                "实际增长_数值": growth,
                "事件时间": rules.format_datetime(moment),
                "登记时间": rules.format_datetime(datetime.now()),
            }
            events.append(event)
            self._rebuild_records()
            return dict(event), f"监控事件「{event_code}」已登记", False

    def list_events(
        self,
        *,
        keyword: str | None = None,
        metric: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock():
            rows = [dict(row) for row in store.rows(EVENT_MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("事件编号", ""))]
        if metric:
            rows = [row for row in rows if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)]
        rows.sort(key=lambda row: (row.get("事件时间", ""), int(row.get("id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _deduped_events(self) -> list[dict[str, Any]]:
        events = store.rows(EVENT_MODULE)
        seen_codes: set[str] = set()
        seen_keys: set[tuple[str, str, float]] = set()
        result: list[dict[str, Any]] = []
        for row in events:
            code = _text(row.get("事件编号"))
            if code and code in seen_codes:
                continue
            moment = rules.parse_datetime(row.get("事件时间"), default=datetime.now())
            growth = rules.growth_value(row.get("实际增长"))
            key = (rules.normalize_text(row.get("监控指标")), rules.format_datetime(moment), growth or 0.0)
            if key in seen_keys:
                continue
            if code:
                seen_codes.add(code)
            seen_keys.add(key)
            result.append({**row, "moment": moment, "growth": growth})
        result.sort(key=lambda item: (item["moment"], int(item.get("id", 0))))
        return result

    # ------------------------------------------------------------ 触发窗口重算
    def recalculate(
        self,
        *,
        rule_code: str | None = None,
        metric: str | None = None,
    ) -> dict[str, Any]:
        """按当前规则版本重算触发窗口；同一静默窗口只落一条记录。"""
        records = self._rebuild_records(rule_code=rule_code, metric=metric)
        summaries = self._summaries(records)
        return {
            "ok": True,
            "message": "触发窗口已按当前条件重算，静默窗口内只保留一条触发记录",
            "total": len(records),
            "items": records,
            "核对编号": summaries,
            "record_nos": [record["触发记录号"] for record in records],
        }

    def trigger_window(self, entry_id: int) -> dict[str, Any] | None:
        """单条规则当前版本的触发窗口视图。"""
        self._ensure_records()
        with store.lock():
            row = store.find(MODULE, entry_id)
            if row is None:
                return None
            # 只有已启用规则参与触发；暂停 / 草稿 / 关闭的规则不产生触发记录。
            active = _text(row.get("status")) == "已启用"
            records = self._records_for_rule(row) if active else []
            summary = self._summaries().get(row.get("规则编号"), {})
            check_no = summary.get("核对编号") if active else f"{row.get('规则编号')}-T0"
            return {
                "ok": True,
                "规则编号": row.get("规则编号"),
                "规则版本": int(row.get("version", 1)),
                "监控指标": row.get("监控指标"),
                "告警条件": row.get("告警条件"),
                "增长阈值": self._threshold_label(row),
                "静默周期_秒": self._silence_of(row),
                "生效范围": (
                    self._scope_of(row).normalized
                    if self._scope_of(row) is not None
                    else _text(rules.pick(row, "生效范围", "生效时段", "effective_scope"))
                ),
                "触发记录数": len(records),
                "核对编号": check_no,
                "items": records,
            }

    def list_trigger_records(
        self,
        *,
        keyword: str | None = None,
        rule_code: str | None = None,
        metric: str | None = None,
        include_history: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        if not include_history:
            self._ensure_records()
        with store.lock():
            rows = [dict(row) for row in store.rows(HISTORY_MODULE if include_history else RECORD_MODULE)]
        if rule_code:
            rows = [row for row in rows if _text(row.get("规则编号")) == rule_code]
        if metric:
            rows = [row for row in rows if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("触发记录号", ""))
                or keyword in str(row.get("监控指标", ""))
            ]
        rows.sort(key=lambda row: (row.get("触发依据时间", ""), int(row.get("id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total, self._summaries()

    def _rebuild_records(
        self,
        *,
        rule_code: str | None = None,
        metric: str | None = None,
    ) -> list[dict[str, Any]]:
        """触发窗口的唯一计算入口：事件 + 当前生效规则版本 -> 触发记录。

        按规则或指标过滤重算时只返回对应记录，但触发表的整表重算始终以全量规则
        与全量事件为准，避免局部重算把别的规则的记录冲掉。
        """
        with store.lock():
            self._records_ready = True
            events = self._deduped_events()
            active = [
                row for row in store.rows(MODULE)
                if _text(row.get("status")) == "已启用"
            ]
            retained = self._retained_rules(active)

            now = rules.format_datetime(datetime.now())
            all_records: list[dict[str, Any]] = []
            for rule in retained:
                all_records.extend(self._records_for_rule(rule, events=events, now=now))
            all_records.sort(key=lambda row: (row["触发依据时间"], row["规则编号"], row["id"]))

            self._archive_replaced_records(all_records)
            records_table = store.rows(RECORD_MODULE)
            records_table.clear()
            records_table.extend(all_records)

            selected = all_records
            if rule_code:
                selected = [row for row in selected if _text(row.get("规则编号")) == rule_code]
            if metric:
                selected = [
                    row for row in selected
                    if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)
                ]
            return selected

    def _records_for_rule(
        self,
        rule: dict[str, Any],
        *,
        events: list[dict[str, Any]] | None = None,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        condition = _text(rule.get("告警条件"))
        parsed = rules.parse_condition(condition)
        if parsed is None:
            return []
        percent = self._percent_of(rule, enforce=False)
        if percent is None:
            return []
        silence = self._silence_of(rule)
        scope = self._scope_of(rule)
        if silence is None or scope is None:
            return []

        rule_metric = rules.normalize_text(rule.get("监控指标"))
        events = self._deduped_events() if events is None else events
        metric_events = [event for event in events if rules.normalize_text(event.get("监控指标")) == rule_metric]
        if not metric_events:
            return []

        # 事件序号在同一指标的事件序列中固定，不随阈值变化而漂移，
        # 因此 ALAR-0002 的触发记录号在版本变化后仍然稳定。
        candidates: list[dict[str, Any]] = []
        for position, event in enumerate(metric_events):
            actual = event.get("growth")
            moment = event["moment"]
            if not scope.allows(moment):
                continue
            if not rules.breached(actual, percent):
                continue
            candidates.append({
                "position": position,
                "event": event,
                "moment": moment,
                "actual": actual,
            })

        windows: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for candidate in candidates:
            if current is None or candidate["moment"] >= current["cutoff"]:
                current = {
                    "start": candidate["moment"],
                    "cutoff": candidate["moment"] + timedelta(seconds=silence),
                    "members": [candidate],
                }
                windows.append(current)
            else:
                current["members"].append(candidate)

        records: list[dict[str, Any]] = []
        for window in windows:
            basis = window["members"][-1]
            event_codes = [_text(member["event"].get("事件编号")) for member in window["members"]]
            records.append({
                "id": 0,
                "触发记录号": f"{rule.get('规则编号')}-T{basis['position']}",
                "规则编号": rule.get("规则编号"),
                "规则版本": int(rule.get("version", 1)),
                "监控指标": rule.get("监控指标"),
                "告警条件": condition,
                "触发依据条件": condition,
                "增长阈值": self._threshold_label(rule),
                "growth_threshold": percent,
                "阈值数值": percent,
                "静默周期": rules.silence_text(silence),
                "静默周期_秒": silence,
                "silence": silence,
                "生效范围": scope.normalized,
                "effective_scope": scope.normalized,
                "窗口开始时间": rules.format_datetime(window["start"]),
                "静默截止时间": rules.format_datetime(window["cutoff"]),
                "触发依据时间": rules.format_datetime(basis["moment"]),
                "触发依据增长幅度": f"{basis['actual']:g}%",
                "触发依据事件号": _text(basis["event"].get("事件编号")),
                "归并事件号": event_codes,
                "归并事件数": len(window["members"]),
                "触发状态": "已生效",
                "status": "已生效",
                "判定说明": (
                    f"实际增长 {basis['actual']:g}% ≥ 阈值 {percent:g}%；"
                    f"静默窗口内 {len(window['members'])} 个事件归并为一条，保留最近触发依据"
                ),
                "created_at": now or rules.format_datetime(datetime.now()),
            })
        for index, record in enumerate(records, start=1):
            record["id"] = index
        return records

    def _retained_rules(
        self,
        active: list[dict[str, Any]],
        events: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """同一监控指标只保留最近触发规则，其余同指标规则不重复落记录。

        优先保留最近一次触发依据最晚的规则；都没有触发记录时，退回版本最新
        （版本号、更新时间、登记顺序依次比较）的规则，保证判定标准唯一。
        """
        by_metric: dict[str, list[dict[str, Any]]] = {}
        for row in active:
            by_metric.setdefault(rules.normalize_text(row.get("监控指标")), []).append(row)

        retained: list[dict[str, Any]] = []
        for metric_rows in by_metric.values():
            if len(metric_rows) == 1:
                retained.append(metric_rows[0])
                continue
            latest_basis: dict[int, str] = {}
            for row in metric_rows:
                records = self._records_for_rule(row, events=events)
                latest_basis[int(row.get("id", 0))] = (
                    records[-1].get("触发依据时间", "") if records else ""
                )
            triggered = [row for row in metric_rows if latest_basis[int(row.get("id", 0))]]
            if triggered:
                retained.append(
                    max(
                        triggered,
                        key=lambda row: (latest_basis[int(row.get("id", 0))], self._rule_rank(row)),
                    )
                )
            else:
                retained.append(max(metric_rows, key=self._rule_rank))
        return retained

    def _latest_basis_map(
        self,
        active: list[dict[str, Any]],
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """每条规则最近一次触发依据时间，用于「保留最近触发规则」的说明。"""
        result: dict[str, str] = {}
        for row in active:
            records = self._records_for_rule(row, events=events)
            result[_text(row.get("规则编号"))] = records[-1].get("触发依据时间", "") if records else ""
        return result

    @staticmethod
    def _rule_rank(row: dict[str, Any]) -> tuple[int, str, int]:
        return (int(row.get("version", 1)), _text(row.get("updated_at")), int(row.get("id", 0)))

    def _archive_replaced_records(self, new_records: list[dict[str, Any]]) -> None:
        """新旧版本交替时把被替换的记录留档，旧记录仍可按原条件读取。

        规则暂停后重新启用时，同一记录会再次出现在生效清单里，此时把对应的留档
        条目移除，避免历史里同时存在「已生效」与「已失效」两条互相矛盾的记录。
        """
        active_keys = {
            (record["触发记录号"], record["规则版本"], record["触发依据事件号"])
            for record in new_records
        }
        history = store.rows(HISTORY_MODULE)
        history[:] = [
            row for row in history
            if (row.get("触发记录号"), row.get("规则版本"), row.get("触发依据事件号")) not in active_keys
        ]
        history_keys = {
            (row.get("触发记录号"), row.get("规则版本"), row.get("触发依据事件号"))
            for row in history
        }
        for record in store.rows(RECORD_MODULE):
            key = (record.get("触发记录号"), record.get("规则版本"), record.get("触发依据事件号"))
            if key in active_keys or key in history_keys:
                continue
            archived = dict(record)
            archived["id"] = max((int(row.get("id", 0)) for row in history), default=0) + 1
            archived["触发状态"] = "已失效"
            archived["status"] = "已失效"
            archived["失效原因"] = "规则版本或触发条件变化，按新条件重算"
            archived["失效时间"] = rules.format_datetime(datetime.now())
            history.append(archived)

    def _ensure_records(self) -> None:
        with store.lock():
            if self._records_ready and store.rows(RECORD_MODULE):
                return
            self._records_ready = True
            self._rebuild_records()

    # ------------------------------------------------------------ 展示与核对
    def _rule_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        status = _text(row.get("status"))
        view["status"] = status
        view["规则状态"] = status
        view["规则版本"] = int(row.get("version", 1))
        view["version"] = int(row.get("version", 1))
        view["增长阈值"] = self._threshold_label(row)
        view["增长阈值_数值"] = self._percent_of(row, enforce=False)
        view["growth_threshold"] = self._percent_of(row, enforce=False)
        view["静默周期"] = (
            rules.silence_text(self._silence_of(row))
            if self._silence_of(row) is not None
            else row.get("静默周期")
        )
        view["静默周期_秒"] = self._silence_of(row)
        view["silence"] = self._silence_of(row)
        view["silence_seconds"] = self._silence_of(row)
        scope = self._scope_of(row)
        view["生效范围"] = (
            scope.normalized
            if scope is not None
            else _text(rules.pick(row, "生效范围", "生效时段", "effective_scope"))
        )
        view["effective_scope"] = view["生效范围"]
        view["监控条件"] = self._condition_view(row)
        summary = self._summaries().get(row.get("规则编号"), {})
        view["触发记录数"] = summary.get("触发记录数", 0)
        view["触发记录号"] = summary.get("触发记录号", "")
        view["核对编号"] = summary.get("核对编号", self._check_no(row))
        view["告警触发结果"] = summary.get("告警触发结果", "未触发")
        view["触发结果"] = view["告警触发结果"]
        view["最近触发规则"] = summary.get("最近触发规则", False)
        view["保留最近触发规则"] = summary.get("保留最近触发规则", "")
        return view

    def _summaries(self, records: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
        """每条规则的触发概览与核对编号；核对编号形如 ALAR-0002-T2 / ALAR-0003-T0。"""
        if records is None:
            records = [dict(row) for row in store.rows(RECORD_MODULE)]
        retained = {
            rules.normalize_text(row.get("监控指标")): _text(row.get("规则编号"))
            for row in self._retained_rules([
                row for row in store.rows(MODULE) if _text(row.get("status")) == "已启用"
            ])
        }
        result: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            rule_code = _text(row.get("规则编号"))
            row_records = [record for record in records if _text(record.get("规则编号")) == rule_code]
            status = _text(row.get("status"))
            # 只有已启用规则才参与触发；暂停 / 草稿 / 关闭的规则核对编号固定为 T0。
            count = len(row_records) if status == "已启用" else 0
            latest = row_records[-1] if (row_records and status == "已启用") else None
            if latest is not None:
                record_no = _text(latest.get("触发记录号"))
                result_text = "触发"
                basis = latest.get("触发依据时间")
            else:
                record_no = ""
                result_text = "未触发"
                basis = ""
            retained_code = retained.get(rules.normalize_text(row.get("监控指标")), "")
            result[rule_code] = {
                "规则编号": rule_code,
                "规则版本": int(row.get("version", 1)),
                "规则状态": status,
                "监控指标": row.get("监控指标"),
                "告警条件": row.get("告警条件"),
                "增长阈值": self._threshold_label(row),
                "静默周期_秒": self._silence_of(row),
                "生效范围": (
                    self._scope_of(row).normalized
                    if self._scope_of(row) is not None
                    else _text(rules.pick(row, "生效范围", "生效时段", "effective_scope"))
                ),
                "触发记录数": count,
                "触发记录号": record_no,
                "核对编号": record_no or f"{rule_code}-T0",
                "最近触发时间": basis,
                "告警触发结果": result_text,
                "最近触发规则": bool(retained_code) and retained_code == rule_code,
                "保留最近触发规则": (
                    f"同一监控指标「{row.get('监控指标')}」保留最近触发规则 {retained_code}"
                    if retained_code else ""
                ),
            }
        return result

    def _check_no(self, row: dict[str, Any]) -> str:
        records = [record for record in store.rows(RECORD_MODULE) if _text(record.get("规则编号")) == _text(row.get("规则编号"))]
        if _text(row.get("status")) != "已启用" or not records:
            return f"{row.get('规则编号')}-T0"
        return _text(records[-1].get("触发记录号"))

    def _percent_of(self, row: dict[str, Any], *, enforce: bool = False) -> float | None:
        raw = rules.pick(row, "增长阈值", "growth_threshold")
        if raw is not None:
            try:
                return rules.parse_growth_threshold(raw, enforce_limit=enforce)[0]
            except rules.StandardError:
                parsed = rules.parse_condition(row.get("告警条件"))
                return parsed["threshold_percent"] if parsed else None
        parsed = rules.parse_condition(row.get("告警条件"))
        return parsed["threshold_percent"] if parsed else None

    def _threshold_label(self, row: dict[str, Any]) -> str:
        percent = self._percent_of(row, enforce=False)
        if percent is not None:
            return rules.threshold_text(percent)
        return _text(row.get("增长阈值"))

    def _silence_of(self, row: dict[str, Any], *, enforce: bool = False) -> int | None:
        raw = rules.pick(row, "静默周期_秒", "silence_seconds", "silence")
        if raw is None:
            raw = rules.pick(row, "静默周期")
        if raw is None:
            return rules.DEFAULT_SILENCE_SECONDS
        try:
            return rules.parse_silence(raw, enforce_limit=enforce)
        except rules.StandardError:
            return None

    def _scope_of(self, row: dict[str, Any]) -> rules.Scope | None:
        raw = rules.pick(row, "生效范围", "生效时段", "effective_scope", default=rules.DEFAULT_SCOPE)
        try:
            return rules.parse_scope(raw)
        except rules.StandardError:
            return None

    def _condition_view(self, row: dict[str, Any]) -> str:
        parsed = rules.parse_condition(row.get("告警条件"))
        percent = self._percent_of(row, enforce=False)
        if parsed is None:
            return _text(row.get("告警条件"))
        if percent is not None:
            return rules.rebuild_condition(row.get("告警条件"), percent)
        return _text(row.get("告警条件"))

    # ------------------------------------------------------------ 异动归因页联动
    def anomaly_trigger_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """指标异动归因页的触发结果：按当前规则版本实时判定，不复用旧结果。"""
        self._ensure_records()
        metric = _text(entry.get("指标名称"))
        matched_rules = [
            row for row in store.rows(MODULE)
            if rules.normalize_text(row.get("监控指标")) == rules.normalize_text(metric)
        ]
        if not matched_rules:
            return {
                "触发记录号": "",
                "告警触发结果": "未配置告警规则",
                "判定条件": "",
                "判定阈值": "",
                "静默周期_秒": None,
                "生效范围": "",
                "最近触发依据": "",
                "触发说明": "该指标尚未配置分析告警规则，不参与触发判定",
            }

        # 同一指标优先按最近触发规则判定；没有启用规则时退回暂停/草稿规则作说明。
        active = [row for row in matched_rules if _text(row.get("status")) == "已启用"]
        retained = self._retained_rules(active)
        rule = retained[0] if retained else max(matched_rules, key=self._rule_rank)
        summary = self._summaries().get(_text(rule.get("规则编号")), {})
        condition = _text(rule.get("告警条件"))
        percent = self._percent_of(rule, enforce=False)
        silence = self._silence_of(rule)
        scope = self._scope_of(rule)
        base = {
            "规则编号": rule.get("规则编号"),
            "规则版本": int(rule.get("version", 1)),
            "监控指标": rule.get("监控指标"),
            "判定条件": condition,
            "判定阈值": rules.threshold_text(percent) if percent is not None else "",
            "增长阈值": rules.threshold_text(percent) if percent is not None else "",
            "静默周期_秒": silence,
            "生效范围": (
                scope.normalized if scope is not None
                else _text(rules.pick(rule, "生效范围", "生效时段", "effective_scope"))
            ),
            "告警触发结果": "未触发",
            "触发记录号": (
                f"{rule.get('规则编号')}-T0"
                if _text(rule.get("status")) != "已启用"
                else summary.get("核对编号", self._check_no(rule))
            ),
            "最近触发依据": "",
            "触发说明": "",
            "核对编号": (
                f"{rule.get('规则编号')}-T0"
                if _text(rule.get("status")) != "已启用"
                else summary.get("核对编号", self._check_no(rule))
            ),
        }

        if _text(rule.get("status")) != "已启用":
            base["触发记录号"] = f"{rule.get('规则编号')}-T0"
            base["核对编号"] = base["触发记录号"]
            base["告警触发结果"] = "未触发"
            base["触发说明"] = (
                f"规则当前为{rule.get('status')}，不参与触发；"
                f"同一监控指标保留最近触发规则为 {summary.get('保留最近触发规则') or '无'}"
            )
            return base
        if percent is None or silence is None or scope is None or rules.parse_condition(condition) is None:
            base["触发说明"] = "告警条件或阈值无法解析，本指标不参与触发判定"
            return base

        moment = rules.parse_datetime(
            rules.pick(entry, "异动时间", "事件时间"),
            default=datetime.now(),
        )
        actual = rules.growth_value(rules.pick(entry, "异动幅度", "实际增长"))
        event_code = _text(rules.pick(entry, "记录编号", "事件编号"))
        moment_text = rules.format_datetime(moment)
        in_scope = scope.allows(moment)
        hit = rules.breached(actual, percent)
        records = self._records_for_rule(rule)
        # 异动记录与触发窗口按 指标 + 时间 对齐：窗口是 [窗口开始, 静默截止) 的半开区间，
        # 窗口内任意一次异动都属于同一条触发记录，记录号不随记录编号命名变化。
        matched_record = next(
            (
                record for record in records
                if record.get("窗口开始时间", "")
                <= moment_text
                < record.get("静默截止时间", "")
            ),
            None,
        )

        if not in_scope:
            base["触发说明"] = f"异动时间 {rules.format_datetime(moment)} 不在生效范围 {scope.normalized} 内，不触发"
            return base
        if not hit:
            base["触发说明"] = (
                f"实际增长 {actual if actual is not None else '—'}% < 阈值 {percent:g}%，未触发"
            )
            return base

        base["最近触发依据"] = rules.format_datetime(moment)
        if matched_record is None:
            base["触发说明"] = "已命中当前条件，但触发窗口尚未重算，请先重算触发窗口"
            return base
        base["触发记录号"] = matched_record["触发记录号"]
        base["核对编号"] = matched_record["触发记录号"]
        is_basis = (
            _text(matched_record.get("触发依据事件号")) == event_code
            or (
                matched_record.get("触发依据时间") == moment_text
                and rules.growth_value(matched_record.get("触发依据增长幅度")) == actual
            )
        )
        if is_basis:
            base["告警触发结果"] = "触发"
            base["触发说明"] = (
                f"命中条件，作为静默窗口内最近一次触发依据并入 {matched_record['触发记录号']}"
            )
        else:
            base["告警触发结果"] = "静默合并"
            base["触发说明"] = (
                f"同一静默窗口内重复异动，只生效一次，已合并到 {matched_record['触发记录号']}"
            )
        return base


service = AlarmService()
