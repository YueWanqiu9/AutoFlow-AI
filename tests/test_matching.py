"""规则匹配、实时指标与页面测试；仅使用临时数据库和虚构人员。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from database.seed_staff import seed_demo_staff
from modules import ai_client, ai_logging, ai_parser, dashboard, events, matching, matching_rules, staff


class MatchingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self.parser_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client_sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.ticket = {
            "id": "matching-test", "customer_input": "虚构护理需求", "service_type": "老人护理",
            "address": "演示甲区示例小区", "appointment_time": "明天下午", "customer_name": "测试客户",
            "phone": "", "status": "待确认", "created_time": "2026-10-14T10:00:00+08:00",
            "priority_final": "高", "priority_suggested": "高",
        }

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def add_staff(self, **kwargs):
        return staff.create_staff(**{
            "name": "匹配测试人员（虚构）", "skills": ["老人护理"], "service_area": "演示甲区", **kwargs,
        })

    def save_ticket(self, **kwargs):
        ticket = {**self.ticket, **kwargs}
        db.save_ticket(ticket)
        return db.get_ticket(ticket["id"])

    def snapshot_database(self):
        with db._connection() as connection:
            return "\n".join(connection.iterdump())

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click_recommendations(self, app):
        next(item for item in app.button if item.label == "查看推荐").click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_result_has_fixed_format_and_full_match_scores_100(self):
        person = self.add_staff()
        result = matching.match_staff(self.ticket)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]), {"staff_id", "score", "match_details"})
        self.assertEqual(result[0]["staff_id"], person["id"])
        self.assertEqual(result[0]["score"], 100)
        self.assertEqual(len(result[0]["match_details"]), 4)
        self.assertTrue(all(isinstance(item, str) for item in result[0]["match_details"]))

    def test_skill_full_match_and_service_alias_use_one_mapping(self):
        self.add_staff(skills=["养老护理"])
        ticket = {**self.ticket, "service_type": " 护理员上门 "}
        self.assertEqual(matching_rules.get_required_skill(ticket), "老人护理")
        self.assertEqual(matching.match_staff(ticket)[0]["score"], 100)
        self.assertIn("技能完全匹配", matching.match_staff(ticket)[0]["match_details"][0])
        for service, required in (("康复服务", "康复辅助"), ("保洁服务", "居家保洁"), ("上门维修", "维修服务")):
            self.assertEqual(matching_rules.get_required_skill({"service_type": service}), required)

    def test_partial_skill_scores_20_and_states_need_to_verify(self):
        person = self.add_staff(skills=["康复辅助"])
        scores = matching_rules.score_staff(self.ticket, person)
        self.assertEqual(scores["skill_score"], 20)
        self.assertEqual(scores["score"], 80)
        self.assertIn("需核实", scores["match_details"][0])
        self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 80)

    def test_multiple_skills_and_duplicates_do_not_accumulate_points(self):
        person = self.add_staff(skills=["康复辅助", "老人护理", "助浴", "老人护理"])
        self.assertEqual(matching_rules.score_staff(self.ticket, person)["skill_score"], 40)
        self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 100)
        staff.update_staff(person["id"], skills=["康复辅助", "助浴"])
        self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 80)

    def test_unmatched_skill_is_zero_and_not_rescued_by_other_points(self):
        person = self.add_staff(skills=["居家保洁"])
        scores = matching_rules.score_staff(self.ticket, person)
        self.assertEqual(scores["skill_score"], 0)
        self.assertEqual(scores["score"], 60)
        self.assertEqual(matching.match_staff(self.ticket), [])

    def test_unknown_skill_requires_exact_match_not_substring(self):
        self.add_staff(skills=["空调清洗"])
        self.assertEqual(matching.match_staff({**self.ticket, "service_type": "空调清洗"})[0]["score"], 100)
        self.assertEqual(matching.match_staff({**self.ticket, "service_type": "无需空调清洗"}), [])
        self.assertEqual(matching.match_staff({**self.ticket, "service_type": "未知综合服务"}), [])

    def test_json_skill_snapshot_uses_unified_parser(self):
        person = self.add_staff()
        pool = [{**person, "skills": json.dumps(["老人护理"], ensure_ascii=False)}]
        self.assertEqual(matching.match_staff(self.ticket, staff_pool=pool)[0]["score"], 100)

    def test_area_exact_and_address_prefix_score_30(self):
        for address in ("演示甲区", "演示甲区示例小区", " 演示甲区 示例小区 "):
            self.assertEqual(matching_rules.score_area(address, "演示甲区")[0], 30)
        self.assertEqual(matching_rules.score_area("自定义服务片区", "自定义服务片区")[0], 30)

    def test_configured_area_aliases_support_full_address_spelling(self):
        self.assertEqual(matching_rules.score_area("云南省玉溪市红塔区示例小区", "红塔区")[0], 30)
        self.assertEqual(matching_rules.score_area("红塔区示例小区", "玉溪市红塔区")[0], 30)
        self.assertEqual(matching_rules.score_area("玉溪市红塔区示例小区", "玉溪市")[0], 30)

    def test_no_area_substring_or_unlisted_parent_inference(self):
        for address, area in (("其他区红塔区路示例小区", "红塔区"), ("红塔区示例小区", "红"),
                              ("红塔区示例小区", "玉溪市"), ("演示甲区示例小区", "演示乙区")):
            with self.subTest(address=address, area=area):
                self.assertEqual(matching_rules.score_area(address, area)[0], 0)

    def test_adjacent_areas_are_configurable_symmetric_and_score_15(self):
        self.assertEqual(matching_rules.ADJACENT_AREAS, ())
        with patch.object(matching_rules, "ADJACENT_AREAS", (("演示甲区", "演示乙区"),)):
            self.assertEqual(matching_rules.score_area("演示乙区小区", "演示甲区")[0], 15)
            self.assertEqual(matching_rules.score_area("演示甲区小区", "演示乙区")[0], 15)
            self.assertEqual(matching_rules.score_area("演示甲区小区", "演示甲区")[0], 30)
            self.assertEqual(matching_rules.score_area("演示丙区小区", "演示甲区")[0], 0)
            self.add_staff(service_area="演示乙区")
            self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 85)

    def test_missing_address_or_service_area_scores_zero_without_claiming_match(self):
        self.add_staff()
        for address in (None, "", " "):
            result = matching.match_staff({**self.ticket, "address": address})[0]
            self.assertEqual(result["score"], 70)
            self.assertIn("需补充", result["match_details"][1])
        self.assertEqual(matching_rules.score_area("演示甲区", "")[0], 0)

    def test_mismatched_area_remains_candidate_with_explicit_warning(self):
        self.add_staff(service_area="演示乙区")
        result = matching.match_staff(self.ticket)[0]
        self.assertEqual(result["score"], 70)
        self.assertIn("区域未匹配", result["match_details"][1])

    def test_status_scores_available_busy_inactive(self):
        for status, expected in (("available", 20), ("busy", 5), ("inactive", 0)):
            self.assertEqual(matching_rules.score_status(status)[0], expected)
        person = self.add_staff(status="busy")
        result = matching.match_staff(self.ticket)[0]
        self.assertEqual(result["staff_id"], person["id"])
        self.assertEqual(result["score"], 85)
        self.assertIn("当前忙碌", result["match_details"][2])

    def test_inactive_people_do_not_enter_candidate_pool(self):
        self.add_staff(status="inactive")
        self.assertEqual(matching.match_staff(self.ticket), [])

    def test_base_points_are_10_and_total_is_sum_of_components(self):
        person = self.add_staff(skills=["助浴"], status="busy", service_area="演示乙区")
        scores = matching_rules.score_staff(self.ticket, person)
        self.assertEqual(scores["base_score"], 10)
        self.assertEqual(scores["score"], 35)
        self.assertEqual(scores["score"], sum(scores[key] for key in ("skill_score", "area_score", "status_score", "base_score")))

    def test_results_are_sorted_by_score_descending_then_id_ascending(self):
        partial = self.add_staff(skills=["助浴"])
        full_first = self.add_staff()
        busy = self.add_staff(status="busy")
        full_second = self.add_staff()
        result = matching.match_staff(self.ticket)
        self.assertEqual([item["staff_id"] for item in result], [full_first["id"], full_second["id"], busy["id"], partial["id"]])
        self.assertEqual([item["score"] for item in result], [100, 100, 85, 80])
        pool = list(reversed(staff.list_staff()))
        self.assertEqual(matching.match_staff(self.ticket, staff_pool=pool), result)

    def test_empty_pool_and_missing_service_return_empty_list(self):
        self.assertEqual(matching.match_staff(self.ticket), [])
        self.add_staff()
        for value in (None, "", "  ", {"value": "老人护理"}):
            self.assertEqual(matching.match_staff({**self.ticket, "service_type": value}), [])
        self.assertEqual(matching.match_staff(self.ticket, staff_pool=[]), [])

    def test_invalid_input_or_malformed_skill_data_reports_error(self):
        with self.assertRaises(ValueError):
            matching.match_staff("not a ticket")
        with self.assertRaises(ValueError):
            matching.match_staff(self.ticket, include_demo="false")
        person = self.add_staff()
        with self.assertRaises(ValueError):
            matching.match_staff(self.ticket, staff_pool=[{**person, "skills": "bad JSON"}])

    def test_demo_people_are_visible_but_can_be_excluded(self):
        seed_demo_staff()
        ticket = {**self.ticket, "service_type": "护理员上门", "address": "红塔区示例小区"}
        self.assertEqual(len(matching.match_staff(ticket)), 2)
        self.assertEqual(matching.match_staff(ticket, include_demo=False), [])
        self.assertEqual(matching.match_staff(ticket, staff_pool=staff.list_staff(), include_demo=False), [])

    def test_matching_does_not_mutate_input_ticket_or_staff_snapshot(self):
        person = self.add_staff()
        pool = [person]
        before_ticket, before_pool = deepcopy(self.ticket), deepcopy(pool)
        result = matching.match_staff(self.ticket, staff_pool=pool)
        result[0]["match_details"].append("仅修改返回值")
        self.assertEqual(self.ticket, before_ticket)
        self.assertEqual(pool, before_pool)

    def test_matching_does_not_change_database_schema_tickets_people_or_events(self):
        ticket = self.save_ticket()
        self.add_staff()
        before = self.snapshot_database()
        for _ in range(2):
            self.assertEqual(matching.match_staff(ticket)[0]["score"], 100)
        self.assertEqual(self.snapshot_database(), before)
        self.assertEqual(staff.list_staff()[0]["status"], "available")
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_matching_ignores_phone_and_never_changes_priority(self):
        self.add_staff()
        expected = matching.match_staff(self.ticket)
        for phone in (None, "", "测试电话"):
            ticket = {**self.ticket, "phone": phone}
            self.assertEqual(matching.match_staff(ticket), expected)
            self.assertEqual(ticket["priority_final"], "高")

    def test_recalculation_reads_current_person_state_and_skill(self):
        person = self.add_staff()
        self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 100)
        staff.update_staff_status(person["id"], "busy")
        self.assertEqual(matching.match_staff(self.ticket)[0]["score"], 85)
        staff.update_staff(person["id"], skills=["居家保洁"])
        self.assertEqual(matching.match_staff(self.ticket), [])

    def test_no_ai_requests_or_ai_log_changes(self):
        self.add_staff()
        self.save_ticket()
        ai_logging.LOG_PATH.write_text("[]", encoding="utf-8")
        matching.match_staff(self.ticket)
        dashboard.get_dashboard_stats()
        self.assertEqual(ai_logging.LOG_PATH.read_text(encoding="utf-8"), "[]")
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_matchable_count_uses_same_candidates_and_counts_each_open_ticket_once(self):
        self.add_staff()
        self.add_staff(status="busy")
        self.save_ticket(id="matchable")
        self.save_ticket(id="review", status="人工审核", review_required=True, address="")
        self.save_ticket(id="unrelated", service_type="居家保洁")
        self.save_ticket(id="completed", status="已完成")
        self.save_ticket(id="cancelled", status="已取消")
        self.save_ticket(id="missing-type", service_type="")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["matchable_ticket_count"], 2)
        self.assertEqual(stats["staff_available_count"], 1)
        self.assertIsNone(stats["matching_metrics_error"])

    def test_busy_candidate_counts_as_matchable_but_not_available(self):
        person = self.add_staff(status="busy")
        self.save_ticket()
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["matchable_ticket_count"], 1)
        self.assertEqual(stats["staff_available_count"], 0)
        staff.update_staff_status(person["id"], "inactive")
        self.assertEqual(dashboard.get_dashboard_stats()["matchable_ticket_count"], 0)

    def test_empty_metrics_and_repeated_stats_do_not_persist_results(self):
        self.assertEqual(dashboard.get_dashboard_stats()["matchable_ticket_count"], 0)
        self.save_ticket()
        self.add_staff()
        before = self.snapshot_database()
        for _ in range(2):
            self.assertEqual(dashboard.get_dashboard_stats()["matchable_ticket_count"], 1)
        self.assertEqual(self.snapshot_database(), before)

    def test_dashboard_reads_staff_pool_once_for_many_tickets(self):
        person = self.add_staff()
        tickets = [{**self.ticket, "id": str(index)} for index in range(10)]
        with patch.object(matching, "list_staff", return_value=[person]) as reader:
            self.assertEqual(matching.count_matchable_tickets(tickets), 10)
        reader.assert_called_once_with()

    def test_invalid_skill_json_marks_matching_unavailable_but_preserves_other_metrics(self):
        person = self.add_staff()
        self.save_ticket()
        with db._connection() as connection:
            connection.execute("UPDATE staff SET skills = ? WHERE id = ?", ("bad JSON", person["id"]))
        stats = dashboard.get_dashboard_stats()
        self.assertIsNone(stats["matchable_ticket_count"])
        self.assertTrue(stats["matching_metrics_error"])
        self.assertEqual(stats["total_tickets"], 1)
        self.assertEqual(stats["staff_available_count"], 1)
        self.assertEqual(stats["ai_call_count"], 0)

    def test_page_shows_recommendation_only_on_click_and_does_not_save_it(self):
        self.save_ticket()
        self.add_staff(name="测试候选甲（虚构）")
        before = self.snapshot_database()
        app = self.open_app()
        self.assertFalse(any("第 1 名：" in item.value for item in app.markdown))
        self.click_recommendations(app)
        self.assertTrue(any("第 1 名：测试候选甲" in item.value for item in app.markdown))
        self.assertTrue(any("评分：100 / 100" in item.value for item in app.markdown))
        self.assertTrue(any(item.label == "确认派单" for item in app.button))
        self.assertFalse(any(item.label == "自动派单" for item in app.button))
        self.assertEqual(next(item.value for item in app.metric if item.label == "可匹配工单数量"), "1")
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertFalse(any("第 1 名：" in item.value for item in app.markdown))
        self.assertEqual(self.snapshot_database(), before)
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_page_marks_demo_and_busy_candidates_and_uses_current_scores(self):
        ticket = self.save_ticket(address="红塔区示例小区")
        seed_demo_staff()
        app = self.open_app()
        self.click_recommendations(app)
        self.assertTrue(any("本次候选包含 demo" in item.value for item in app.info))
        self.assertTrue(any("当前忙碌" in item.value for item in app.markdown))
        first = matching.match_staff(ticket)[0]["staff_id"]
        staff.update_staff_status(first, "inactive")
        self.click_recommendations(app)
        self.assertFalse(any("张护理员" in item.value for item in app.markdown if "名：" in item.value))

    def test_page_switching_ticket_does_not_show_previous_candidates(self):
        first = self.save_ticket(id="nursing")
        second = self.save_ticket(id="cleaning", service_type="居家保洁")
        self.add_staff()
        app = self.open_app()
        next(item for item in app.selectbox if item.label == "选择工单").set_value(first["id"]).run(timeout=30)
        self.click_recommendations(app)
        next(item for item in app.selectbox if item.label == "选择工单").set_value(second["id"]).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertFalse(any("第 1 名：" in item.value for item in app.markdown))
        self.click_recommendations(app)
        self.assertTrue(any("暂无匹配人员" in item.value for item in app.info))

    def test_page_handles_read_failure_and_bad_skill_data_without_crashing(self):
        self.save_ticket()
        person = self.add_staff()
        app = self.open_app()
        with patch.object(matching, "match_staff", side_effect=sqlite3.OperationalError("test read failure")):
            self.click_recommendations(app)
            self.assertTrue(any("人员推荐读取失败" in item.value for item in app.error))
        with db._connection() as connection:
            connection.execute("UPDATE staff SET skills = ? WHERE id = ?", ("invalid", person["id"]))
        self.click_recommendations(app)
        self.assertTrue(any("人员资料格式不正确" in item.value for item in app.error))
        self.assertEqual(next(item.value for item in app.metric if item.label == "可匹配工单数量"), "暂不可用")


if __name__ == "__main__":
    unittest.main()
