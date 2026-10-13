"""跨模块业务验收：真实 Demo 入口到服务完成，以及人员占用一致性。"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from database.seed_staff import seed_demo_staff
from modules import advisor, ai_client, ai_logging, ai_parser, conversation, dashboard, dispatch, events, matching, recommendation, service_tracking as tracking, staff, tickets


class BusinessAcceptanceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "test.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "log.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self.parser_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client_sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.fields = {"service_type": "老人护理", "customer_name": "演示客户", "phone": "演示电话001",
                       "address": "示例区演示小区", "appointment_time": "明天下午两点", "urgency": "普通"}
        self.person = staff.create_staff("验收人员（虚构）", skills=["老人护理"], service_area="示例区", is_demo=True)

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def snapshot(self):
        with db._connection() as connection:
            return "\n".join(connection.iterdump())

    def create_dispatch(self):
        ticket = tickets.create_ticket("虚构验收需求", self.fields)
        dispatch_id = dispatch.create_dispatch(ticket["id"], self.person["id"], "验收运营", "已核对", busy_confirmed=True)
        return ticket, dispatch_id

    def complete(self, dispatch_id):
        tracking.accept_dispatch(dispatch_id)
        tracking.start_service(dispatch_id)
        tracking.complete_service(dispatch_id, "验收服务完成")

    def test_demo_reply_review_priority_advice_matching_dispatch_service_closed_loop(self):
        parsed = ai_parser.parse_customer_request(config.DEMO_CUSTOMER_REQUEST)
        ticket = tickets.create_ticket(config.DEMO_CUSTOMER_REQUEST, parsed)
        original_parse = ticket["confidence_score"]
        self.assertTrue(ticket["review_required"])
        conversation.start_conversation(ticket["id"])
        filled = conversation.process_reply(ticket["id"], "姓名：演示客户；电话：演示电话001")
        self.assertEqual(conversation.detect_missing_fields(filled)["missing"], [])
        with self.assertRaisesRegex(ValueError, "人工审核"):
            dispatch.create_dispatch(ticket["id"], self.person["id"], "验收运营", "尚未审核")
        reviewed = tickets.review_ticket(ticket["id"], {key: filled[key] for key in tickets.TICKET_FIELDS})
        self.assertEqual(reviewed["confidence_score"], original_parse)
        tickets.confirm_priority(ticket["id"], "普通", "人工核对时间与风险")
        generated = tickets.generate_ticket_advice(ticket["id"])
        final_advice = advisor.format_advice(json.loads(generated["advice_json"]))
        reviewed = tickets.confirm_advice(ticket["id"], "accepted", final_advice, "已核对建议")
        candidates = matching.match_staff(reviewed)
        self.assertTrue(candidates, "固定 Demo 的上门护理应能匹配老人护理技能")
        before_explanation = self.snapshot()
        explanation = recommendation.generate_recommendation_explanation(reviewed, candidates)
        self.assertEqual(explanation["recommended_staff"], self.person["name"])
        self.assertEqual(self.snapshot(), before_explanation)
        dispatch_id = dispatch.create_dispatch(ticket["id"], candidates[0]["staff_id"], "验收运营", "已确认范围和时间")
        self.complete(dispatch_id)
        final_ticket = db.get_ticket(ticket["id"])
        self.assertEqual(final_ticket, reviewed)
        self.assertEqual(len(db.get_all_tickets()), 1)
        record = dispatch.get_ticket_dispatches(ticket["id"])[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["is_demo"], 1)
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")
        stats = dashboard.get_dashboard_stats()
        for key, expected in {"total_tickets": 1, "dispatch_total_count": 1, "service_completed_count": 1,
                              "staff_available_count": 1, "review_required_count": 0, "completed_conversation_count": 1,
                              "advice_generated_count": 1, "ai_call_count": 0}.items():
            self.assertEqual(stats[key], expected, key)
        kinds = [item["event_type"] for item in events.get_ticket_events(ticket["id"])]
        for kind in ("ticket_created", "field_filled", "ticket_reviewed", "priority_confirmed", "advice_reviewed",
                     "dispatch_created", "staff_assigned", "dispatch_accepted", "service_started", "service_completed"):
            self.assertIn(kind, kinds)
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_manual_available_rejected_for_every_open_service_state(self):
        for status in ("confirmed", "accepted", "in_service", "exception"):
            with self.subTest(status=status):
                _, dispatch_id = self.create_dispatch()
                if status != "confirmed":
                    tracking.accept_dispatch(dispatch_id)
                if status == "in_service":
                    tracking.start_service(dispatch_id)
                if status == "exception":
                    tracking.mark_exception(dispatch_id, "验收异常")
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "未结束派单"):
                    staff.update_staff_status(self.person["id"], "available", reason="误操作释放")
                self.assertEqual(self.snapshot(), before)

    def test_fixed_demo_matches_the_default_seed_staff_pool(self):
        with patch.object(db, "DB_PATH", db.DB_PATH.with_name("seed-pool.db")):
            parsed = ai_parser.parse_customer_request(config.DEMO_CUSTOMER_REQUEST)
            ticket = tickets.create_ticket(config.DEMO_CUSTOMER_REQUEST, parsed)
            self.assertEqual(seed_demo_staff(), 3)
            candidates = matching.match_staff(ticket)
            self.assertTrue(candidates)
            self.assertEqual(staff.get_staff(candidates[0]["staff_id"])["name"], "张护理员（演示）")
            self.assertTrue(any("技能完全匹配" in detail for detail in candidates[0]["match_details"]))
            self.assertTrue(any("区域" in detail and "0分" in detail for detail in candidates[0]["match_details"]))

    def test_legacy_available_staff_is_reserved_when_progress_is_recorded(self):
        for target in ("accepted", "in_service", "exception"):
            with self.subTest(target=target):
                _, dispatch_id = self.create_dispatch()
                if target != "accepted":
                    tracking.accept_dispatch(dispatch_id)
                # 模拟旧版本曾允许产生的矛盾记录，不代表公开接口可以绕过保护。
                with db._connection() as connection:
                    connection.execute("UPDATE staff SET status='available' WHERE id=?", (self.person["id"],))
                action = {"accepted": lambda: tracking.accept_dispatch(dispatch_id),
                          "in_service": lambda: tracking.start_service(dispatch_id),
                          "exception": lambda: tracking.mark_exception(dispatch_id, "现场异常")}[target]
                action()
                self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
                event = events.get_staff_events(self.person["id"])[-1]
                self.assertEqual(event["before_data"], {"status": "available"})
                self.assertEqual(event["after_data"], {"status": "busy"})

    def test_legacy_reservation_rolls_back_with_progress_event(self):
        ticket, dispatch_id = self.create_dispatch()
        with db._connection() as connection:
            connection.execute("UPDATE staff SET status='available' WHERE id=?", (self.person["id"],))
        before = self.snapshot()
        with patch.object(events, "create_event", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(sqlite3.OperationalError):
                tracking.accept_dispatch(dispatch_id)
        self.assertEqual(self.snapshot(), before)

    def test_cancel_reassign_complete_preserves_history_and_does_not_release_other_staff(self):
        ticket, first_id = self.create_dispatch()
        tracking.accept_dispatch(first_id)
        tracking.mark_exception(first_id, "原人员无法继续")
        tracking.cancel_dispatch(first_id, "与客户确认重新安排")
        replacement = staff.create_staff("替补人员（虚构）", skills=["老人护理"], service_area="示例区", is_demo=True)
        second_id = dispatch.create_dispatch(ticket["id"], replacement["id"], "验收运营", "人工重新安排")
        self.complete(second_id)
        records = dispatch.get_ticket_dispatches(ticket["id"])
        self.assertEqual([item["status"] for item in records], ["completed", "cancelled"])
        self.assertEqual(records[1]["exception_reason"], "原人员无法继续")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
        self.assertEqual(staff.get_staff(replacement["id"])["status"], "available")
        # 全部未结束派单已消除，运营可在核对后手动恢复原人员。
        staff.update_staff_status(self.person["id"], "available", reason="已确认原人员空闲")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["dispatch_total_count"], 2)
        self.assertEqual(stats["dispatch_cancelled_count"], 1)
        self.assertEqual(stats["service_completed_count"], 1)
        self.assertEqual(stats["service_exception_count"], 0)
        self.assertEqual(db.get_ticket(ticket["id"])["status"], "待确认")

    def test_inactive_staff_with_open_dispatch_cannot_be_reactivated_as_available(self):
        self.create_dispatch()
        staff.update_staff_status(self.person["id"], "inactive", reason="暂停服务")
        with self.assertRaisesRegex(ValueError, "未结束派单"):
            staff.update_staff_status(self.person["id"], "available")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "inactive")
        self.assertEqual(staff.update_staff_status(self.person["id"], "busy")["status"], "busy")

    def test_concurrent_manual_release_and_assignment_cannot_leave_open_dispatch_available(self):
        ticket = tickets.create_ticket("虚构需求", self.fields)
        def assign():
            return dispatch.create_dispatch(ticket["id"], self.person["id"], "运营", "确认服务")
        def release():
            try:
                return staff.update_staff_status(self.person["id"], "available")
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(assign), pool.submit(release)]
            results = [task.result() for task in tasks]
        self.assertIsInstance(results[0], int)
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")

    def test_page_fixed_demo_can_be_reviewed_matched_and_completed(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        def click(label):
            next(item for item in app.button if item.label == label).click().run(timeout=30)
            self.assertFalse(app.exception)
        click("一键演示")
        for label, value in (("客户姓名", "演示客户"), ("电话", "演示电话001")):
            next(item for item in app.text_input if item.label == label).set_value(value)
        next(item for item in app.checkbox if item.label == "我已核对客户原始需求与字段").check()
        click("审核通过并保存")
        click("查看推荐")
        self.assertTrue(any("第 1 名：" in item.value for item in app.markdown))
        click("确认派单")
        next(item for item in app.text_input if item.label == "确认人").set_value("演示运营")
        next(item for item in app.text_area if item.label == "派单原因").set_value("核对服务范围与时间")
        next(item for item in app.checkbox if item.label == "我已核对服务需求、服务范围和预约时间").check()
        click("确认并创建派单")
        click("确认接单")
        click("开始服务")
        next(item for item in app.text_area if item.label == "完成备注").set_value("演示流程验收完成")
        click("完成服务")
        self.assertEqual(next(item.value for item in app.metric if item.label == "已完成服务数量"), "1")
        self.assertEqual(next(item.value for item in app.metric if item.label == "可用人员数量"), "1")
        before = self.snapshot()
        app.run(timeout=30)
        self.assertEqual(self.snapshot(), before)
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
