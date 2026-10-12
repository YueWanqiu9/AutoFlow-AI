"""工单管理页面的离线交互检查，使用临时 SQLite 和模拟解析。"""

import json
import os
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_logging, ai_parser, automation
from modules.tickets import create_ticket


class TicketManagementTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini"}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        fixed_now = datetime(2026, 10, 11, 12, tzinfo=automation.REPORT_TIMEZONE)
        for target in ("modules.automation.datetime", "modules.tickets.datetime", "modules.dashboard.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now
        self.fields = {
            "service_type": "空调清洗",
            "appointment_time": "周六上午",
            "address": "示例路1号",
            "customer_name": "演示客户",
            "phone": "演示电话001",
            "urgency": "普通",
        }
        self.parser = self._patch(
            patch("modules.ai_parser.parse_customer_request", return_value={
                field: {"value": value, "confidence": 0.95 if value else 0.0}
                for field, value in self.fields.items()
            })
        )

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def choose(self, app, label, value):
        next(item for item in app.selectbox if item.label == label).set_value(value)

    def submit(self, app):
        next(item for item in app.button if item.label == "保存状态").click().run(timeout=30)
        self.assertFalse(app.exception)

    def daily_metrics(self, app):
        return [next(metric.value for metric in app.metric if metric.label == label)
                for label in ("新增", "已完成", "待处理")]

    def test_empty_database_has_no_status_controls(self):
        app = self.open_app()
        self.assertFalse(app.selectbox)
        self.assertFalse(app.dataframe)
        self.assertTrue(any("暂无历史工单" in item.value for item in app.info))
        self.parser.assert_not_called()

    def test_table_shows_all_tickets_and_switching_uses_selected_current_status(self):
        first = create_ticket("第一条", self.fields)
        second = create_ticket("第二条", {**self.fields, "phone": ""})
        app = self.open_app()
        table = app.dataframe[0].value
        self.assertEqual(
            list(table.columns),
            ["工单编号", "服务类型", "地址", "客户姓名", "电话", "当前状态", "创建时间"],
        )
        self.assertEqual(set(table["工单编号"]), {first["id"], second["id"]})
        self.assertEqual(app.selectbox[1].value, "待补充信息")
        self.choose(app, "选择工单", first["id"])
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox[1].value, "待确认")
        self.assertEqual(len(db.get_all_tickets()), 2)
        self.parser.assert_not_called()

    def test_all_six_statuses_save_only_selected_ticket_and_refresh(self):
        selected = create_ticket("操作目标", self.fields)
        other = create_ticket("另一条工单", self.fields)
        app = self.open_app()
        self.choose(app, "选择工单", selected["id"])
        app.run(timeout=30)
        statuses = ("待补充信息", "待确认", "待派单", "服务中", "已完成", "异常")
        self.assertEqual(set(app.selectbox[1].options), {*statuses, "人工审核"})
        with patch("database.db.update_ticket_status", wraps=db.update_ticket_status) as updater:
            for index, status in enumerate(statuses):
                with self.subTest(status=status):
                    self.choose(app, "修改状态", status)
                    self.submit(app)
                    self.assertEqual(updater.call_count, index + 1)
                    rows = {row["id"]: row for row in db.get_all_tickets()}
                    self.assertEqual(rows[selected["id"]], {**selected, "status": status})
                    self.assertEqual(rows[other["id"]], other)
                    table = app.dataframe[0].value.set_index("工单编号")
                    self.assertEqual(table.loc[selected["id"], "当前状态"], status)
                    self.assertEqual(app.selectbox[1].value, status)
                    self.assertTrue(any("工单状态已更新" in item.value for item in app.success))
        fresh = self.open_app()
        table = fresh.dataframe[0].value.set_index("工单编号")
        self.assertEqual(table.loc[selected["id"], "当前状态"], "异常")
        self.assertEqual(len(db.get_all_tickets()), 2)
        self.parser.assert_not_called()

    def test_current_ticket_and_report_refresh_after_status_update(self):
        app = self.open_app()
        app.text_area[0].set_value("请安排空调清洗。")
        next(item for item in app.button if item.label == "创建工单").click().run(timeout=30)
        self.assertFalse(app.exception)
        ticket_id = json.loads(app.json[1].value)["id"]
        self.choose(app, "修改状态", "已完成")
        self.submit(app)
        self.assertEqual(json.loads(app.json[1].value)["status"], "已完成")
        self.assertEqual(db.get_all_tickets()[0]["id"], ticket_id)
        self.assertEqual(self.daily_metrics(app), ["1", "1", "0"])
        self.assertEqual(self.parser.call_count, 1)

    def test_save_failure_does_not_claim_success_or_change_data(self):
        ticket = create_ticket("测试工单", self.fields)
        app = self.open_app()
        self.choose(app, "修改状态", "异常")
        with patch("database.db.update_ticket_status", side_effect=sqlite3.OperationalError("test")):
            self.submit(app)
        self.assertTrue(any("状态保存失败" in item.value for item in app.error))
        self.assertFalse(app.success)
        self.assertEqual(db.get_all_tickets(), [ticket])
        self.parser.assert_not_called()

    def test_missing_selected_ticket_shows_warning(self):
        ticket = create_ticket("测试工单", self.fields)
        app = self.open_app()
        self.choose(app, "修改状态", "异常")
        with patch("database.db.update_ticket_status", return_value=False):
            self.submit(app)
        self.assertTrue(any("未找到所选工单" in item.value for item in app.warning))
        self.assertFalse(app.success)
        self.assertEqual(db.get_all_tickets(), [ticket])

    def test_unchanged_status_does_not_write_database(self):
        create_ticket("测试工单", self.fields)
        app = self.open_app()
        with patch("database.db.update_ticket_status") as updater:
            self.submit(app)
            updater.assert_not_called()
        self.assertTrue(any("状态未变化" in item.value for item in app.info))

    def test_legacy_status_requires_explicit_target_before_save(self):
        ticket = create_ticket("测试工单", self.fields)
        db.update_ticket_status(ticket["id"], "待处理")
        app = self.open_app()
        self.assertIsNone(app.selectbox[1].value)
        with patch("database.db.update_ticket_status") as updater:
            self.submit(app)
            updater.assert_not_called()
        self.assertTrue(any("请选择目标状态" in item.value for item in app.warning))
        self.choose(app, "修改状态", "待派单")
        self.submit(app)
        self.assertEqual(db.get_all_tickets()[0]["status"], "待派单")
        self.assertEqual(self.daily_metrics(app), ["1", "0", "1"])
        self.parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
