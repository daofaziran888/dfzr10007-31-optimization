"""分析告警触发窗口的回归测试。

覆盖口径：
* ALAR-0002 的核对值：增长阈值 15%、静默周期 1800 秒；
* 同一监控指标连续异动在静默窗口内只落一条记录，记录号为 ALAR-0002-T2；
* ALAR-0003 保持暂停，核对编号 ALAR-0003-T0，不参与触发；
* 重复事件只生效一次；
* 阈值变化后旧版本按原条件可读，新窗口按新条件计算；
* 静默周期 / 增长阈值 / 生效范围越界时拒绝进入生效状态。
"""
from __future__ import annotations

import unittest

from app.services import alarm_rules as rules
from app.services.alarm import AlarmService
from app.store import store


class AlarmTriggerWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._backup = {name: [dict(row) for row in rows] for name, rows in store._tables.items()}
        self.service = AlarmService()

    def tearDown(self) -> None:
        store._tables = {name: [dict(row) for row in rows] for name, rows in self._backup.items()}

    def test_rule_calibration_values(self) -> None:
        entry = self.service.get_entry(2)
        self.assertEqual(entry["规则编号"], "ALAR-0002")
        self.assertEqual(entry["增长阈值"], "15%")
        self.assertEqual(entry["静默周期_秒"], 1800)
        self.assertEqual(entry["growth_threshold"], 15.0)
        self.assertEqual(entry["silence"], 1800)
        self.assertEqual(entry["status"], "已启用")

    def test_alarm_0003_stays_paused(self) -> None:
        entry = self.service.get_entry(3)
        self.assertEqual(entry["规则编号"], "ALAR-0003")
        self.assertEqual(entry["status"], "已暂停")
        self.assertEqual(entry["核对编号"], "ALAR-0003-T0")
        window = self.service.trigger_window(3)
        self.assertEqual(window["触发记录数"], 0)

    def test_silence_window_keeps_single_record(self) -> None:
        result = self.service.recalculate()
        records = [row for row in result["items"] if row["规则编号"] == "ALAR-0002"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["触发记录号"], "ALAR-0002-T2")
        self.assertEqual(record["触发依据时间"], "2026-09-02 09:26:00")
        self.assertEqual(record["触发依据增长幅度"], "22%")
        self.assertEqual(record["归并事件数"], 3)
        self.assertEqual(record["增长阈值"], "15%")
        self.assertEqual(record["静默周期_秒"], 1800)
        # 静默窗口 [09:00, 09:30)，窗口内的重复异动只保留最近一次触发依据。
        self.assertEqual(record["窗口开始时间"], "2026-09-02 09:00:00")
        self.assertEqual(record["静默截止时间"], "2026-09-02 09:30:00")
        self.assertEqual(result["核对编号"]["ALAR-0002"]["核对编号"], "ALAR-0002-T2")
        self.assertEqual(result["核对编号"]["ALAR-0003"]["核对编号"], "ALAR-0003-T0")

    def test_duplicate_event_only_once(self) -> None:
        event = {
            "事件编号": "EVT-DUP-001",
            "监控指标": "订单GMV",
            "实际增长": "30%",
            "事件时间": "2026-09-02 13:00:00",
        }
        first, _, duplicate_first = self.service.create_event(dict(event))
        second, _, duplicate_second = self.service.create_event(dict(event))
        self.assertFalse(duplicate_first)
        self.assertTrue(duplicate_second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(store.rows("alarm_events")), 7)
        records = [row for row in store.rows("alarm_trigger_records") if row["规则编号"] == "ALAR-0002"]
        # 13:00 距离 09:00 的窗口已过静默截止，允许新增一条，但重复提交不再叠加。
        self.assertEqual(len(records), 2)

    def test_threshold_change_keeps_old_version_readable(self) -> None:
        entry, message = self.service.add_version(2, {"增长阈值": "20%", "变更说明": "阈值调整为20%"})
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["增长阈值"], "20%")
        self.assertEqual(entry["规则版本"], 3)

        versions = {row["规则版本"]: row for row in self.service.list_versions(2)}
        self.assertEqual(versions[1]["增长阈值"], "10%")
        self.assertEqual(versions[2]["增长阈值"], "15%")
        self.assertEqual(versions[3]["增长阈值"], "20%")

        # 新窗口按新条件计算：09:00 的 18% 不再单独触发，窗口从 09:26 的 22% 开始。
        records = [row for row in store.rows("alarm_trigger_records") if row["规则编号"] == "ALAR-0002"]
        self.assertTrue(records)
        self.assertTrue(all(row["增长阈值"] == "20%" for row in records))
        self.assertEqual(records[0]["触发记录号"], "ALAR-0002-T2")
        self.assertEqual(records[0]["窗口开始时间"], "2026-09-02 09:26:00")

        # 旧版本记录留档，仍按原条件可读。
        history = store.rows("alarm_trigger_history")
        self.assertTrue(any(row["规则版本"] == 2 and row["增长阈值"] == "15%" for row in history))

    def test_silence_above_limit_rejected(self) -> None:
        entry, _ = self.service.create_entry({
            "规则编号": "ALAR-0101",
            "监控指标": "支付转化率",
            "告警条件": "支付转化率 同环比增长率 >= 5%",
            "静默周期": "7200秒",
        })
        self.assertIsNotNone(entry)
        result, message = self.service.run_action(entry["id"], "启用规则")
        self.assertIsNone(result)
        self.assertIn("静默周期", message)
        self.assertEqual(store.find("alarm", entry["id"])["status"], "草稿")

    def test_threshold_above_limit_rejected(self) -> None:
        entry, message = self.service.create_entry({
            "规则编号": "ALAR-0102",
            "监控指标": "退款率",
            "告警条件": "退款率 同环比增长率 >= 150%",
        })
        self.assertIsNone(entry)
        self.assertIn("无法解析", message)

    def test_scope_out_of_range_rejected(self) -> None:
        entry, _ = self.service.create_entry({
            "规则编号": "ALAR-0103",
            "监控指标": "支付转化率",
            "告警条件": "支付转化率 同环比增长率 >= 5%",
            "生效范围": "25:00-26:00",
        })
        self.assertIsNotNone(entry)
        result, message = self.service.run_action(entry["id"], "启用规则")
        self.assertIsNone(result)
        self.assertIn("生效范围", message)

    def test_scope_limits_trigger_window(self) -> None:
        # 09:00-09:26 都落在工作日 09:00-10:00 内；12:00 之后的事件不参与触发。
        entry, _ = self.service.create_entry({
            "规则编号": "ALAR-0104",
            "监控指标": "支付转化率",
            "告警条件": "支付转化率 同环比增长率 >= 5%",
            "静默周期": "30分钟",
            "生效范围": "工作日 09:00-10:00",
        })
        self.service.run_action(entry["id"], "启用规则")
        records = [
            row for row in store.rows("alarm_trigger_records")
            if row["规则编号"] == "ALAR-0104"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["触发依据时间"], "2026-09-02 09:30:00")
        self.assertEqual(records[0]["生效范围"], "工作日 09:00-10:00")

    def test_anomaly_page_uses_current_condition(self) -> None:
        from app.services.anomaly import AnomalyService

        view = {row["记录编号"]: row for row in AnomalyService().list_entries()[0]}
        self.assertEqual(view["ANOM-0002"]["告警触发结果"], "触发")
        self.assertEqual(view["ANOM-0002"]["触发记录号"], "ALAR-0002-T2")
        self.assertEqual(view["ANOM-0001"]["告警触发结果"], "静默合并")
        self.assertEqual(view["ANOM-0003"]["告警触发结果"], "未触发")
        self.assertEqual(view["ANOM-0002"]["判定阈值"], "15%")

        # 阈值改为 25% 后页面立即按新条件判定，不再复用旧结果。
        self.service.add_version(2, {"增长阈值": "25%"})
        view = {row["记录编号"]: row for row in AnomalyService().list_entries()[0]}
        self.assertEqual(view["ANOM-0002"]["判定阈值"], "25%")
        self.assertEqual(view["ANOM-0002"]["告警触发结果"], "未触发")

    def test_same_metric_keeps_latest_trigger_rule(self) -> None:
        # 默认数据里 ALAR-0003 暂停，订单GMV 只有 ALAR-0002 参与触发。
        default_summary = self.service.get_entry(2)
        self.assertTrue(default_summary["最近触发规则"])

        # 人为启用同指标的 ALAR-0003（阈值 5%，触发到 11:00）后，只保留最近触发规则，
        # 同一指标不再出现两套记录。
        store.find("alarm", 3)["status"] = "已启用"
        records = self.service.recalculate()["items"]
        rule_codes = {row["规则编号"] for row in records}
        self.assertEqual(len(rule_codes), 1)
        retained_code = next(iter(rule_codes))
        summary = self.service.get_entry(3 if retained_code == "ALAR-0003" else 2)
        self.assertTrue(summary["最近触发规则"])
        self.assertIn(retained_code, summary["保留最近触发规则"])
        # 保留的应是触发依据最晚的那条规则（ALAR-0003 最近触发依据为 11:00）。
        latest_basis = max(row["触发依据时间"] for row in records)
        self.assertEqual(latest_basis, "2026-09-02 11:00:00")


class AlarmRulesTest(unittest.TestCase):
    def test_threshold_parsing(self) -> None:
        self.assertEqual(rules.parse_growth_threshold("15%")[0], 15.0)
        self.assertEqual(rules.parse_growth_threshold(15)[0], 15.0)
        self.assertEqual(rules.parse_growth_threshold("15 个百分点")[0], 15.0)
        with self.assertRaises(rules.StandardError):
            rules.parse_growth_threshold("0%")
        with self.assertRaises(rules.StandardError):
            rules.parse_growth_threshold("150%")

    def test_silence_parsing(self) -> None:
        self.assertEqual(rules.parse_silence("1800"), 1800)
        self.assertEqual(rules.parse_silence("1800秒"), 1800)
        self.assertEqual(rules.parse_silence("30分钟"), 1800)
        self.assertEqual(rules.parse_silence("1小时"), 3600)
        with self.assertRaises(rules.StandardError):
            rules.parse_silence("2小时")

    def test_scope_parsing(self) -> None:
        self.assertEqual(rules.parse_scope("全天").normalized, "全天")
        self.assertEqual(rules.parse_scope("工作日 09:00-18:00").normalized, "工作日 09:00-18:00")
        with self.assertRaises(rules.StandardError):
            rules.parse_scope("25:00-26:00")


if __name__ == "__main__":
    unittest.main()
