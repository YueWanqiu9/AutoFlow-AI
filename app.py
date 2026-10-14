"""AutoFlow AI 的 Streamlit 入口；业务逻辑放在 modules 中。"""

import hashlib
import json
import sqlite3

import streamlit as st

import config
from database.db import get_all_tickets, get_conversation_state, init_db
from database.seed import seed_demo_data
from database.seed_staff import seed_demo_staff
from modules.ai_parser import AIParserError, get_ai_status, parse_customer_request, parse_demo_request
from modules.analytics import get_operational_metrics
from modules.advisor import ADVICE_LABELS, DECISION_LABELS, format_advice, validate_advice
from modules.automation import generate_daily_report
from modules.dashboard import get_dashboard_stats
from modules.demo_view import show_full_demo, show_home_dashboard, show_technology_overview
from modules.dispatch import STATUS_LABELS as DISPATCH_STATUS_LABELS, create_dispatch, dispatch_snapshot, get_ticket_dispatches
from modules.service_tracking import accept_dispatch, start_service, complete_service, mark_exception, cancel_dispatch
from modules.events import get_ticket_events
from modules.conversation import FIELD_LABELS, detect_missing_fields, process_reply, start_conversation
from modules.rules import CRITICAL_FIELDS, check_confidence, get_confidence_score
from modules.staff import STATUS_LABELS as STAFF_STATUS_LABELS, list_staff
from modules.matching import match_staff
from modules.recommendation import generate_recommendation_explanation
from modules.tickets import (
    change_ticket_status, confirm_advice, confirm_priority, create_ticket, generate_ticket_advice, generate_ticket_priority,
    get_saved_parse_result, review_ticket,
)


def fill_demo_request():
    st.session_state["customer_request"] = config.DEMO_CUSTOMER_REQUEST


def start_demo():
    fill_demo_request()
    st.session_state["run_one_click_demo"] = True


def show_demo_steps(ticket, parsed_result):
    st.subheader("完整 Demo 流程")
    st.caption("本次使用模拟解析，展示需求到工单的完整流程；未调用真实 AI。")
    with st.container(border=True):
        st.subheader("步骤1：AI解析结果")
        st.json(parsed_result, expanded=1)
    with st.container(border=True):
        st.subheader("步骤2：字段置信度")
        labels = {
            "service_type": "服务类型", "appointment_time": "预约时间", "address": "地址",
            "customer_name": "客户姓名", "phone": "电话", "urgency": "紧急程度",
        }
        st.table([
            {"字段": label, "解析值": parsed_result[field]["value"] or "待补充",
             "置信度": f"{parsed_result[field]['confidence']:.0%}",
             "关键字段": "是" if field in CRITICAL_FIELDS else "否"}
            for field, label in labels.items()
        ])
        st.caption("演示分数为预设数据；真实模式的模型自评分也不等于经过验证的准确率。")
    with st.container(border=True):
        st.subheader("步骤3：规则判断")
        initial_status = check_confidence(parsed_result)
        st.write(f"初始规则判断：{initial_status}")
        st.write(f"关键字段最低分：{get_confidence_score(parsed_result):.0%}；审核阈值：70%。")
        st.write("关键字段为服务类型、预约时间、地址和电话。")
        if initial_status == "人工审核":
            pending_fields = [labels[field] for field in CRITICAL_FIELDS
                              if not parsed_result[field]["value"] or parsed_result[field]["confidence"] < 0.7]
            st.write(f"需要核对或补充：{'、'.join(pending_fields)}。")
        else:
            st.write("关键字段均完整且达到阈值，可进入待确认。")
    with st.container(border=True):
        st.subheader("步骤4：生成工单")
        st.write(f"工单编号：{ticket['id']}")
        st.success("工单已保存到 SQLite，可在下方工单管理中继续处理。")
        st.json(ticket, expanded=False)
    with st.container(border=True):
        st.subheader("步骤5：进入审核/确认")
        st.write(f"当前状态：{ticket['status']}")
        show_confidence(ticket)
        if ticket["review_required"]:
            st.warning("请在下方审核表单核对原始需求、补齐电话，并勾选确认后保存。")
        elif initial_status == "人工审核":
            st.success(f"人工审核已完成，当前工单状态为“{ticket['status']}”。")
        else:
            st.success(f"无需人工审核，当前工单状态为“{ticket['status']}”。")


def show_confidence(ticket):
    parsed_result = get_saved_parse_result(ticket)
    if parsed_result:
        score = get_confidence_score(parsed_result)
        st.write(f"AI可信度（关键字段最低分）：{score:.0%}（原始分数：{score}）")
    else:
        st.write("AI可信度：未记录")
    if ticket.get("review_required", False):
        review_status = "需要人工审核"
    elif parsed_result and check_confidence(parsed_result) == "人工审核":
        review_status = "已完成人工审核"
    elif parsed_result:
        review_status = "无需人工审核"
    else:
        review_status = "历史工单未记录 AI 可信度"
    st.write(f"审核状态：{review_status}")


def show_ticket_history(ticket_id):
    with st.expander("操作历史", expanded=True):
        try:
            history = get_ticket_events(ticket_id)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            st.warning("操作历史暂不可读取，请稍后重试。")
            return
        if not history:
            st.info("暂无操作历史。历史工单在启用事件记录前的操作未保存。")
            return
        event_labels = {
            "ticket_created": "创建工单", "status_changed": "状态修改",
            "ticket_reviewed": "人工审核", "demo_imported": "演示数据导入",
            "priority_generated": "生成优先级建议", "priority_confirmed": "确认优先级",
            "advice_generated": "生成处理建议", "advice_reviewed": "评审处理建议",
            "conversation_started": "开始信息补全", "conversation_question_generated": "生成补充问题",
            "field_filled": "补充字段", "conversation_completed": "信息补全完成",
            "dispatch_created": "确认派单", "staff_assigned": "关联服务人员",
            "dispatch_accepted": "人员接单", "service_started": "开始服务",
            "service_completed": "完成服务", "service_exception": "服务异常", "dispatch_cancelled": "取消派单",
        }
        actor_labels = {"system": "系统", "operator": "运营操作", "demo": "演示导入"}
        field_labels = {
            "service_type": "服务类型", "appointment_time": "预约时间", "address": "地址",
            "customer_name": "客户姓名", "phone": "电话", "urgency": "紧急程度",
            "status": "状态", "review_required": "需要人工审核",
            "priority_suggested": "建议等级", "priority_final": "最终等级",
            "priority_reason": "判断依据", "priority_source": "建议来源",
            "advice_json": "原始处理建议", "advice_source": "处理建议来源",
            "advice_decision": "建议评审状态", "advice_final": "最终处理方案", "advice_generated_at": "建议生成时间",
            "message_id": "对话记录编号", "question_field": "本轮询问字段", "missing_fields": "缺失字段",
            "completion_status": "信息完整性", "source": "补充来源",
            "dispatch_id": "派单编号", "staff_id": "人员编号", "staff_status": "人员状态",
            "dispatch_status": "派单状态", "confirmed_by": "确认人", "assigned_at": "派单时间",
            "is_demo": "演示数据", "busy_confirmed": "已明确确认忙碌人员",
            "accepted_at": "接单时间", "started_at": "开始时间", "completed_at": "完成时间",
            "exception_reason": "异常原因", "completed_note": "完成备注",
        }

        def display_value(value, field=None):
            if field == "staff_status":
                return STAFF_STATUS_LABELS.get(value, "未记录")
            if field == "dispatch_status":
                return DISPATCH_STATUS_LABELS.get(value, "未记录")
            if field == "missing_fields" and isinstance(value, list):
                return "、".join(FIELD_LABELS.get(item, item) for item in value) or "无"
            if field == "question_field":
                return FIELD_LABELS.get(value, "无需自动补问")
            if field == "completion_status":
                return {"complete": "完整", "incomplete": "待补全"}.get(value, "未记录")
            if field == "source":
                return {"ai": "AI", "rule": "规则", "demo": "演示规则", "manual": "人工"}.get(value, "未记录")
            if isinstance(value, bool):
                return "是" if value else "否"
            if field == "advice_decision":
                return DECISION_LABELS.get(value, "未记录")
            if field == "advice_source":
                return {"ai": "AI", "rule": "规则模板", "demo": "演示模板"}.get(value, "未记录")
            if field == "advice_json" and value:
                try:
                    return format_advice(json.loads(value))
                except (ValueError, TypeError):
                    return "建议内容格式错误"
            return str(value) if value not in (None, "") else "未填写"

        rows = []
        for event in history:
            before, after = event["before_data"], event["after_data"]
            changes = []
            for field in dict.fromkeys([*before, *after]):
                label = field_labels.get(field, field)
                new_value = display_value(after.get(field), field)
                if field in before and before[field] == after.get(field):
                    context = "操作依据" if event["event_type"] == "staff_assigned" or event["event_type"].startswith(("service_", "dispatch_")) else "评审依据"
                    changes.append(f"{label}（{context}）：{new_value}")
                elif field in before:
                    changes.append(f"{label}：{display_value(before[field], field)} → {new_value}")
                else:
                    changes.append(f"{label}：{new_value}")
            if event["reason"]:
                changes.append(event["reason"])
            rows.append({
                "时间": event["created_at"],
                "事件类型": event_labels.get(event["event_type"], event["event_type"]),
                "操作来源": actor_labels.get(event["actor_type"], event["actor_type"]),
                "变化内容": "；".join(changes),
            })
        st.table(rows)


def show_conversation(ticket):
    st.subheader("信息补全助手")
    missing = detect_missing_fields(ticket)["missing"]
    st.write(f"当前完整度：{(5 - len(missing)) / 5:.0%}")
    st.write("缺失：" + ("、".join(FIELD_LABELS[field] for field in missing) or "无"))
    st.caption("完整度只反映五项必需信息是否有值。补齐不会自动通过人工审核，也不会覆盖已有字段、优先级或最终处理方案。")
    if "appointment_time" in missing:
        st.caption("预约时间尚待确认，可先保存工单；已有人工审核要求继续保留。")
    updated = None
    try:
        state = get_conversation_state(ticket["id"])
        st.caption("补问由规则模板生成；回复按当前模式解析。建议用“字段：内容”作答，不明确的内容会继续询问。")
        if st.button("开始补全", key=f"conversation_start_{ticket['id']}", disabled=not missing or bool(state["messages"])):
            updated = start_conversation(ticket["id"])
        with st.expander("补全对话记录", expanded=bool(state["messages"])):
            if not state["messages"]:
                st.info("点击开始补全后生成问题，不会创建新工单。")
            for message in state["messages"]:
                label = {"system": "系统", "assistant": "补全助手", "user": "客户回复"}[message["role"]]
                st.caption(f"{label} · {message['created_at']}")
                st.text(message["content"])
        actionable = [field for field in missing if field not in state["protected_fields"]]
        if missing and not actionable:
            st.warning("剩余字段由人工明确清空，暂停自动补全，请通过人工审核核对。")
        if state["messages"] and actionable:
            # 表单与问题版本绑定，旧页面提交不能回答到另一轮问题上。
            version = state["last_message_id"]
            shown_key = f"conversation_shown_{ticket['id']}"
            shown_version = st.session_state.get(shown_key, version)
            if shown_version != version:
                st.warning("对话已更新，请查看最新问题后重新提交。")
            with st.form(f"conversation_reply_{ticket['id']}_{version}"):
                reply = st.text_area("客户回复", key=f"conversation_reply_text_{ticket['id']}_{version}",
                                     placeholder="例如：客户姓名：演示客户；联系电话：演示电话002", max_chars=2000)
                submitted = st.form_submit_button("提交回复")
            if submitted:
                with st.spinner("正在核对并补充信息……"):
                    updated = process_reply(ticket["id"], reply, expected_message_id=shown_version)
                if updated is None:
                    st.warning("工单不存在，请刷新页面。")
            st.session_state[shown_key] = version
    except ValueError as error:
        st.warning(str(error))
    except (sqlite3.Error, OSError):
        st.error("信息补全保存失败，本轮修改已回滚，请重试。")
    if updated is not None:
        if st.session_state.get("latest_ticket", {}).get("id") == ticket["id"]:
            st.session_state["latest_ticket"] = updated
        # 重新展示后再记录下一轮版本，避免本人的成功提交显示过期提示。
        st.session_state.pop(f"conversation_shown_{ticket['id']}", None)
        st.session_state["status_update_message"] = "信息补全记录已保存，请查看当前字段及下一步问题。"
        st.rerun()


def show_advice_management(ticket):
    st.subheader("AI处理建议")
    ticket_id = ticket["id"]
    reviewed = ticket.get("advice_decision") not in (None, "unreviewed")
    snapshot_key = f"advice_display_{ticket_id}"
    # 表单提交时核对上一次展示的工单，而非本次重跑刚读取的新数据。
    expected_ticket = st.session_state.get(snapshot_key, ticket)
    if expected_ticket != ticket:
        st.warning("工单已被修改，请核对当前内容后重新提交处理建议评审。")
    updated = None
    try:
        if st.button("更新处理建议" if ticket.get("advice_json") else "生成处理建议",
                     key=f"advice_generate_{ticket_id}", disabled=reviewed):
            with st.spinner("正在生成处理建议……"):
                updated = generate_ticket_advice(ticket_id)
            if updated is None:
                st.warning("工单不存在，请刷新页面。")
        st.caption("仅点击生成按钮才生成建议；采纳建议不会自动执行服务或改变工单状态。")
        if reviewed:
            st.caption("已有评审结果，保留原建议供追溯；可填写原因后再次调整最终方案或拒绝。")
        if ticket.get("advice_json"):
            advice = validate_advice(json.loads(ticket["advice_json"]))
            for field, label in ADVICE_LABELS.items():
                st.markdown(f"**{label}**")
                if not advice[field]:
                    st.write("无")
                for index, item in enumerate(advice[field], 1):
                    st.text(f"{index}. {item}")
            source = {"ai": "AI", "rule": "规则模板", "demo": "演示模板"}.get(ticket.get("advice_source"), "未记录")
            st.write(f"来源：{source}")
            st.caption(f"建议生成时间：{ticket.get('advice_generated_at') or '未记录'}")
            st.write(f"建议评审状态：{DECISION_LABELS.get(ticket.get('advice_decision'), '未评审')}")
            st.markdown("**最终处理方案**")
            st.text(ticket.get("advice_final") or ("已拒绝，未采用方案" if ticket.get("advice_decision") == "rejected" else "尚未确认"))
            original = format_advice(advice)
            version = hashlib.sha256(json.dumps([ticket.get(field) for field in (
                "advice_generated_at", "advice_decision", "advice_final",
            )], ensure_ascii=False).encode()).hexdigest()[:16]
            with st.form(f"advice_review_{ticket_id}_{version}"):
                final_advice = st.text_area("编辑最终处理方案", value=ticket.get("advice_final") or original,
                                            height=200, key=f"advice_final_{ticket_id}_{version}")
                reason = st.text_input("处理建议评审原因", key=f"advice_reason_{ticket_id}_{version}")
                st.caption("采纳保存完整原建议；修改后采纳保存编辑内容；拒绝不采用方案。请填写评审原因。")
                accepted = st.form_submit_button("采纳建议")
                modified = st.form_submit_button("修改后采纳")
                rejected = st.form_submit_button("拒绝建议")
            if accepted or modified or rejected:
                decision = "accepted" if accepted else "modified" if modified else "rejected"
                final = original if accepted else final_advice if modified else ""
                updated = confirm_advice(ticket_id, decision, final, reason, expected_ticket=expected_ticket)
                if updated is None:
                    st.warning("工单不存在，请刷新页面。")
        else:
            st.info("尚未生成处理建议。历史工单不会自动生成，请按需点击生成按钮。")
    except (ValueError, TypeError) as error:
        st.warning(str(error) if not isinstance(error, json.JSONDecodeError) else "处理建议格式错误，请检查保存的数据。")
    except (sqlite3.Error, OSError):
        st.error("处理建议保存失败，本次修改已回滚，请稍后重试。")
    st.session_state[snapshot_key] = dict(ticket)
    if updated is not None:
        if st.session_state.get("latest_ticket", {}).get("id") == ticket_id:
            st.session_state["latest_ticket"] = updated
        st.session_state[snapshot_key] = updated
        st.session_state["status_update_message"] = "处理建议结果已保存。"
        st.rerun()


def show_priority_management(ticket):
    st.subheader("优先级管理")
    suggested = ticket.get("priority_suggested")
    final = ticket.get("priority_final")
    source = {"ai": "AI解释＋规则", "rule": "业务规则", "demo": "演示规则"}.get(ticket.get("priority_source"), "尚未生成")
    st.write(f"AI／规则建议：{suggested or '尚未生成'}")
    st.write(f"理由：{ticket.get('priority_reason') or '尚无判断依据'}")
    st.write(f"来源：{source}")
    st.write(f"最终等级：{final or '待分级'}")
    st.caption("等级由规则确定，AI仅解释依据；不是预测模型。时间临近按未来24小时判断。审核后请核对是否需要更新建议。")

    updated = None
    try:
        if st.button("更新优先级建议" if suggested else "生成优先级建议", key=f"priority_generate_{ticket['id']}"):
            with st.spinner("正在生成优先级建议……"):
                updated = generate_ticket_priority(ticket["id"])
            if updated is None:
                st.warning("工单不存在，请刷新页面。")
        if st.button("确认建议", key=f"priority_accept_{ticket['id']}",
                     disabled=suggested not in ("高", "普通", "低") or bool(final and final != suggested)):
            updated = confirm_priority(ticket["id"], suggested, "运营人员确认系统建议。")
            if updated is None:
                st.warning("工单不存在，请刷新页面。")
        if final and final != suggested:
            st.caption("已有最终等级；如需改为其他等级，请在下方填写原因后保存。")
        with st.form(f"priority_change_{ticket['id']}"):
            priority = st.selectbox("最终优先级", ("高", "普通", "低"),
                                    index=("高", "普通", "低").index(final) if final in ("高", "普通", "低") else None,
                                    placeholder="请选择最终等级")
            reason = st.text_input("确认／修改原因", key=f"priority_reason_{ticket['id']}")
            submitted = st.form_submit_button("修改等级")
        if submitted:
            updated = confirm_priority(ticket["id"], priority, reason)
            if updated is None:
                st.warning("工单不存在，请刷新页面。")
    except ValueError as error:
        st.warning(str(error))
    except (sqlite3.Error, OSError):
        st.error("优先级保存失败，本次修改已回滚，请稍后重试。")
    if updated is not None:
        if st.session_state.get("latest_ticket", {}).get("id") == ticket["id"]:
            st.session_state["latest_ticket"] = updated
        st.session_state["status_update_message"] = "优先级结果已保存。"
        st.rerun()


def clear_dispatch_confirmation():
    st.session_state.pop("pending_dispatch", None)


def prepare_dispatch_confirmation(ticket, person, candidate):
    st.session_state["pending_dispatch"] = {
        "ticket": ticket, "person": person, "candidate": candidate,
        "snapshot": dispatch_snapshot(ticket, person),
    }


@st.dialog("确认派单", on_dismiss=clear_dispatch_confirmation)
def show_dispatch_confirmation(pending):
    ticket, person, candidate = pending["ticket"], pending["person"], pending["candidate"]
    st.write(f"工单：{ticket['id']}")
    st.write(f"推荐人员：{person['name']}（编号 {person['id']}）")
    st.write(f"评分：{candidate['score']} / 100")
    for detail in candidate["match_details"]:
        st.write(f"• {detail}")
    st.write(f"预约时间：{ticket.get('appointment_time') or '未填写'}")
    st.caption("确认后保存派单，并将人员标记为忙碌；不会自动开始服务或通知人员。")
    if person["is_demo"] or ticket["id"].startswith("DEMO-") or ticket.get("priority_source") == "demo":
        st.info("本次为 demo 演示派单，关联的是演示数据。")
    form_key = hashlib.sha256(json.dumps(pending["snapshot"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    with st.form(f"dispatch_form_{form_key}"):
        confirmed_by = st.text_input("确认人", key=f"dispatch_operator_{form_key}")
        reason = st.text_area("派单原因", key=f"dispatch_reason_{form_key}")
        verified = st.checkbox("我已核对服务需求、服务范围和预约时间", key=f"dispatch_verified_{form_key}")
        busy_confirmed = False
        if person["status"] == "busy":
            st.warning("人员当前登记为忙碌。系统未接入排班，请先人工确认时间冲突。")
            busy_confirmed = st.checkbox("我已确认该忙碌人员可以承接本单", key=f"dispatch_busy_{form_key}")
        submit = st.form_submit_button("确认并创建派单")
    if st.button("取消本次确认", key=f"dispatch_cancel_{form_key}"):
        clear_dispatch_confirmation()
        st.rerun()
    if submit:
        if not verified:
            st.error("请先核对服务需求、服务范围和预约时间，并勾选确认。")
            return
        try:
            dispatch_id = create_dispatch(
                ticket["id"], person["id"], confirmed_by, reason,
                busy_confirmed=busy_confirmed, expected_snapshot=pending["snapshot"],
            )
        except ValueError as error:
            st.error(str(error))
        except (sqlite3.Error, OSError):
            st.error("派单保存失败，本次操作未保存，请重试。")
        else:
            clear_dispatch_confirmation()
            st.session_state["dispatch_success"] = (ticket["id"], dispatch_id)
            st.rerun()


def show_service_tracking(records, ticket_id):
    st.subheader("服务状态")
    st.caption("由运营后台登记，独立于工单的人工状态；此处不代表人员手机端确认或实时定位。")
    message = st.session_state.pop("service_update_message", None)
    if message and message[0] == ticket_id:
        st.success(message[1])
    if not records:
        st.info("暂无派单记录，确认派单后才能登记接单和服务进度。")
        return
    record = records[0]
    dispatch_id, status = record["id"], record["status"]
    st.write(f"派单 #{dispatch_id} ｜ 当前状态：{DISPATCH_STATUS_LABELS.get(status, status)}")
    st.table([
        {"节点": label, "登记时间": record.get(field) or "未记录"}
        for label, field in (("派单时间", "assigned_at"), ("接单时间", "accepted_at"),
                             ("开始时间", "started_at"), ("完成时间", "completed_at"))
    ])
    if record.get("exception_reason"):
        st.warning(f"异常原因：{record['exception_reason']}")
    action, args = None, ()
    if status == "confirmed":
        if st.button("确认接单", key=f"service_accept_{dispatch_id}"):
            action, args = accept_dispatch, (dispatch_id,)
    elif status == "accepted":
        if st.button("开始服务", key=f"service_start_{dispatch_id}"):
            action, args = start_service, (dispatch_id,)
    elif status == "in_service":
        with st.form(f"service_complete_{dispatch_id}"):
            note = st.text_area("完成备注", key=f"service_note_{dispatch_id}")
            if st.form_submit_button("完成服务"):
                action, args = complete_service, (dispatch_id, note)
    elif status == "completed":
        st.success("服务已完成，该派单不可再次修改。")
        st.write(f"完成备注：{record.get('completed_note') or '未填写'}")
    elif status == "exception":
        st.info("异常派单不能直接完成；请运营跟进，必要时取消后重新派单。")
    elif status == "cancelled":
        st.info("该派单已取消；取消原因和操作时间可在操作历史中查看。")
    if status in ("accepted", "in_service"):
        with st.form(f"service_exception_{dispatch_id}_{status}"):
            reason = st.text_area("异常原因", key=f"service_exception_reason_{dispatch_id}_{status}")
            if st.form_submit_button("标记异常"):
                action, args = mark_exception, (dispatch_id, reason)
    if status in ("confirmed", "accepted", "in_service", "exception"):
        with st.expander("取消未完成派单"):
            st.caption("取消会终止本派单，但不会自动恢复人员可用；请运营核对人员后续安排。")
            with st.form(f"service_cancel_{dispatch_id}_{status}"):
                reason = st.text_area("取消原因", key=f"service_cancel_reason_{dispatch_id}_{status}")
                confirmed = st.checkbox("我确认取消此派单", key=f"service_cancel_check_{dispatch_id}_{status}")
                if st.form_submit_button("取消派单"):
                    if confirmed:
                        action, args = cancel_dispatch, (dispatch_id, reason)
                    else:
                        st.error("请先勾选确认取消此派单。")
    if action:
        try:
            updated = action(*args)
        except ValueError as error:
            st.error(str(error))
        except (sqlite3.Error, OSError):
            st.error("服务进度保存失败，本次修改已回滚，请稍后重试。")
        else:
            st.session_state["service_update_message"] = (ticket_id, f"服务进度已更新为“{DISPATCH_STATUS_LABELS[updated['status']]}”。")
            st.rerun()
    if len(records) > 1:
        with st.expander("历史派单"):
            st.table([{"派单编号": item["id"], "人员编号": item["staff_id"],
                       "状态": DISPATCH_STATUS_LABELS[item["status"]], "派单时间": item["assigned_at"]}
                      for item in records[1:]])


def show_staff_matching(ticket):
    st.subheader("人员匹配推荐")
    st.caption("规则评分：技能40分、区域30分、状态20分、基础10分。部分相关技能20分、已配置相邻区域15分、忙碌5分。")
    st.caption("只推荐技能相关且未停用的人员。同分按人员编号排序；部分相关、区域未匹配和忙碌候选仍需核实。评分不代表服务成功概率或派单资格。")
    success = st.session_state.pop("dispatch_success", None)
    if success and success[0] == ticket["id"]:
        st.success(f"派单成功，派单编号：{success[1]}。人员已标记忙碌。")
    try:
        dispatches = get_ticket_dispatches(ticket["id"])
    except (sqlite3.Error, OSError):
        st.error("派单记录读取失败，请稍后重试。")
        return
    current_dispatch = next((item for item in dispatches if item["status"] != "cancelled"), None)
    if current_dispatch:
        st.info(f"已确认派单 #{current_dispatch['id']} ｜ 人员编号：{current_dispatch['staff_id']} ｜ 确认人：{current_dispatch['confirmed_by']}")
        st.caption(f"派单时间：{current_dispatch['assigned_at']} ｜ 原因：{current_dispatch['confirm_reason']} ｜ {'demo 演示派单' if current_dispatch['is_demo'] else '业务派单'}")
    show_service_tracking(dispatches, ticket["id"])
    pending = st.session_state.get("pending_dispatch")
    if pending:
        if pending["ticket"]["id"] != ticket["id"] or current_dispatch:
            clear_dispatch_confirmation()
        else:
            show_dispatch_confirmation(pending)
    view_requested = st.button("查看推荐", key=f"staff_matching_{ticket['id']}")
    explain_requested = st.button("生成AI推荐说明", key=f"staff_explanation_{ticket['id']}")
    st.caption("查看推荐只计算规则；生成AI推荐说明会使用当前最新候选，启用真实AI时调用模型。演示模式使用规则说明。")
    if not (view_requested or explain_requested):
        st.caption("点击后按当前资料计算；刷新或切换工单后，请重新查看推荐或生成说明。")
        return
    try:
        # 同一次查询同时用于计算和展示，避免姓名、状态与评分来自不同快照。
        people = list_staff()
        candidates = match_staff(ticket, staff_pool=people)
    except (sqlite3.Error, OSError):
        st.error("人员推荐读取失败，请稍后重试。")
        return
    except ValueError:
        st.error("人员资料格式不正确，暂时无法计算推荐。")
        return
    if not candidates:
        st.info("暂无匹配人员，请核对工单服务类型、人员技能和可用状态。")
        return
    people_by_id = {person["id"]: person for person in people}
    st.caption("结果仅反映本次查看时的登记资料，未校验具体预约档期。相邻区域仅按配置判断，不推断地理距离。")
    if any(people_by_id[item["staff_id"]]["is_demo"] for item in candidates):
        st.info("本次候选包含 demo 演示人员，均为虚构资料。")
    for rank, candidate in enumerate(candidates, start=1):
        person = people_by_id[candidate["staff_id"]]
        with st.container(border=True):
            st.write(f"第 {rank} 名：{person['name']}")
            st.write(f"评分：{candidate['score']} / 100")
            st.caption(f"人员编号：{person['id']} ｜ {STAFF_STATUS_LABELS[person['status']]} ｜ {'demo 演示数据' if person['is_demo'] else '业务录入'}")
            for detail in candidate["match_details"]:
                st.write(f"• {detail}")
            st.button(
                "确认派单", key=f"dispatch_choose_{ticket['id']}_{person['id']}",
                disabled=current_dispatch is not None, on_click=prepare_dispatch_confirmation,
                args=(ticket, person, candidate),
            )
    if explain_requested:
        st.subheader("AI推荐说明")
        with st.spinner("正在整理推荐理由与风险提示……"):
            explanation = generate_recommendation_explanation(ticket, candidates, staff_pool=people)
        st.write(f"推荐人员：{explanation['recommended_staff'] or '暂不可解释，请重新查看推荐'}")
        st.markdown("**推荐理由**")
        for index, reason in enumerate(explanation["reason"], 1):
            st.write(f"{index}. {reason}")
        st.markdown("**风险提示**")
        for note in explanation["risk_notes"]:
            st.write(f"• {note}")
        st.caption(f"来源：{'AI' if explanation['source'] == 'ai' else '规则'}")
        st.caption("对象由规则排序固定；AI只从本次匹配事实中组织说明，不修改评分、人员状态或工单。")


st.set_page_config(page_title="AutoFlow AI", page_icon="🛠️", layout="wide")
st.title("AutoFlow AI")
st.markdown("### 智能服务工单与运营自动化系统")
st.caption("项目负责人：刘宜琛 · 个人AI应用项目 · 方向：AI应用落地 / 运营数字化")
st.caption("将非结构化客户需求，通过AI解析、信息补全、资源匹配和运营决策，转化为可执行服务流程。")
st.warning(
    "公开演示环境：仅限模拟资料。禁止输入真实姓名、电话号码、服务地址、身份证号、API Key 等敏感信息。"
    "工作台由访问者共享，请优先体验首页的业务流程 Demo。"
)
st.sidebar.info("首次访问：在首页体验业务流程 Demo。上方可切换“演示首页／运营工作台”；左侧“项目介绍”说明业务背景，点击“app”可返回首页。")

try:
    init_db()
except (sqlite3.Error, OSError):
    st.error("数据库初始化失败，请检查数据目录的读写权限后重试。")
    st.stop()

home_tab, workbench_tab = st.tabs(["演示首页", "运营工作台"])
with home_tab:
    show_full_demo()
    st.caption(
        "当前版本使用模拟业务数据进行 Demo 展示。由于采用轻量化部署方案，云端环境重启后数据可能重新初始化；"
        "可再次运行 Demo 生成案例。正式业务版本可接入持久化数据库。"
    )
    st.subheader("核心模块")
    modules = (
        ("AI需求解析", "提取服务、时间、地址等结构化字段。"),
        ("工单自动生成", "生成工单编号，保存需求与处理状态。"),
        ("业务规则判断", "检查关键信息和置信度，决定后续处理。"),
        ("人工审核机制", "核对低置信度结果，补齐缺失信息。"),
        ("运营数据分析", "查看新增、待审、完成和真实 AI 调用指标。"),
    )
    for card, (name, description) in zip(st.columns(5), modules):
        with card, st.container(border=True):
            st.markdown(f"**{name}**")
            st.caption(description)

    st.subheader("业务流程")
    st.markdown("**客户需求 → AI解析 → 信息补全 → 运营确认 → 人员匹配 → 服务执行 → 数据分析**")
    st.caption("先理解客户要什么，再确认如何执行，最后追踪服务是否完成。")
    home_dashboard = st.container()
    show_technology_overview()

with workbench_tab:
    st.caption("手动练习路线：需求解析 → 信息补全 → 审核 → 优先级 → 处理建议 → 人员匹配 → 派单 → 服务跟踪。只想快速了解项目，请切换“演示首页”。")
    st.subheader("当前系统状态")
    st.info("当前阶段：已支持需求解析、信息补全、运营审核、人员推荐、确认派单与服务跟踪。")
    ai_status = get_ai_status()
    st.write(f"AI状态：○ {'模拟模式' if ai_status['demo_mode'] else 'GPT实时解析'}")
    if not ai_status["api_key_configured"]:
        st.info("当前为演示模式，未连接真实AI。")
    elif ai_status["demo_mode"]:
        st.info("当前为演示模式，使用模拟解析，不调用真实AI。")
    else:
        st.caption(f"真实模型：{ai_status['model']}。配置已就绪，API 连通性以实际调用结果为准。")
    if ai_status["configuration_error"]:
        st.warning(ai_status["configuration_error"])
    if ai_status["demo_mode"]:
        st.caption("模拟解析仅识别预设 Demo 案例；其他需求保留原文并交由人工审核。")
    else:
        st.caption("点击创建工单会将需求文本发送给 OpenAI，并可能产生 API 费用，请核对返回的信息。")
    st.caption("真实模式的可信度为模型自评分，Demo 为预设分数，均不代表经验证的正确率；关键字段低于 70% 时需要人工审核。")
    st.write("在工单管理区域查看全部工单、核对解析结果并修改状态。")

    st.subheader("客户需求")
    st.button("一键演示", type="primary", on_click=start_demo)
    st.caption("单步练习：创建工单后由你继续审核，数据保存在工作台。完整隔离演示请切换至演示首页。")
    st.button("填入 Demo 案例", on_click=fill_demo_request)
    customer_input = st.text_area(
        "请输入客户需求",
        placeholder="例如：我叫演示客户，想预约周六上午空调清洗，地址：示例路1号，电话：演示电话001，请尽快安排。",
        key="customer_request",
        height=150,
    )

    demo_requested = st.session_state.pop("run_one_click_demo", False)
    if st.button("创建工单") or demo_requested:
        try:
            with st.spinner("正在解析客户需求……"):
                parsed_request = parse_demo_request(customer_input) if demo_requested else parse_customer_request(customer_input)
            ticket = create_ticket(customer_input, parsed_request)
            st.session_state["latest_parsed_request"] = parsed_request
            st.session_state["latest_ticket"] = ticket
            if demo_requested:
                st.session_state["demo_ticket_id"] = ticket["id"]
                st.session_state["managed_ticket_id"] = ticket["id"]
            else:
                st.session_state.pop("demo_ticket_id", None)
        except AIParserError as error:
            st.error(f"{error} 本次未创建工单。")
            if "latest_ticket" in st.session_state:
                st.info("下方展示的是上次成功的解析结果和工单。")
        except ValueError as error:
            st.warning(str(error))
        except (sqlite3.Error, OSError):
            st.error("工单保存失败，本次未创建成功，请检查数据目录权限或稍后重试。")
            if "latest_ticket" in st.session_state:
                st.info("下方展示的是上次成功的解析结果和工单。")
        else:
            st.success("工单已创建并保存。")
            if not ticket.get("priority_suggested"):
                st.warning("工单已保存，优先级建议暂未保存，可在工单详情中重试。")
            if getattr(parsed_request, "call_status", "success") not in ("success", "demo"):
                st.warning(f"AI 解析已降级，工单需要人工审核。{getattr(parsed_request, 'error', '')}")
            if getattr(parsed_request, "logging_error", ""):
                st.warning("工单已保存，但 AI 调用日志写入失败，请检查日志目录。")

    latest_ticket = st.session_state.get("latest_ticket")
    showing_demo = latest_ticket is not None and st.session_state.get("demo_ticket_id") == latest_ticket["id"]
    if showing_demo:
        show_demo_steps(latest_ticket, st.session_state["latest_parsed_request"])

    if "latest_parsed_request" in st.session_state and not showing_demo:
        st.subheader("最近的需求解析结果")
        latest_parse = st.session_state["latest_parsed_request"]
        if getattr(latest_parse, "call_status", "") == "demo":
            st.caption("本次为模拟解析，未调用真实 AI，不计入 AI 调用次数。")
        elif getattr(latest_parse, "api_called", False):
            elapsed = getattr(latest_parse, "response_time", None)
            elapsed_text = f"{elapsed:.2f} 秒" if elapsed is not None else "暂无测量"
            st.caption(f"模型：{latest_parse.model} ｜ token：{latest_parse.token_used} ｜ 请求耗时：{elapsed_text}")
        st.json(latest_parse)

    if "latest_ticket" in st.session_state and not showing_demo:
        st.subheader("最近创建的工单")
        latest_ticket = st.session_state["latest_ticket"]
        st.write(f"当前状态：{latest_ticket['status']}")
        show_confidence(latest_ticket)
        st.json(latest_ticket)

    st.subheader("工单管理")
    if "status_update_message" in st.session_state:
        st.success(st.session_state.pop("status_update_message"))

    try:
        history = get_all_tickets()
    except (sqlite3.Error, OSError):
        st.error("历史工单读取失败，请检查数据库后重试。")
    else:
        if history:
            columns = {
                "id": "工单编号",
                "service_type": "服务类型",
                "address": "地址",
                "customer_name": "客户姓名",
                "phone": "电话",
                "status": "当前状态",
                "created_time": "创建时间",
            }
            rows = [{label: ticket[field] for field, label in columns.items()} for ticket in history]
            st.dataframe(rows, hide_index=True, width="stretch")

            tickets_by_id = {ticket["id"]: ticket for ticket in history}
            selected_id = st.selectbox(
                "选择工单",
                options=list(tickets_by_id),
                format_func=lambda ticket_id: (
                    f"{ticket_id} ｜ {tickets_by_id[ticket_id]['service_type'] or '未填写服务类型'}"
                    f" ｜ {tickets_by_id[ticket_id]['customer_name'] or '未填写姓名'}"
                ),
                key="managed_ticket_id",
            )
            selected_ticket = tickets_by_id[selected_id]
            st.write(f"所选工单当前状态：{selected_ticket['status']}")
            show_confidence(selected_ticket)
            with st.expander("查看客户原始需求与原 AI 解析结果"):
                st.write(selected_ticket["customer_input"])
                saved_result = get_saved_parse_result(selected_ticket)
                if saved_result:
                    st.json(saved_result)
                else:
                    st.info("此历史工单没有保存 AI 可信度。")

            show_conversation(selected_ticket)
            if selected_ticket["review_required"]:
                st.subheader("人工审核")
                st.warning("此工单需人工审核，请核对或修正字段后提交。")
                review_version = hashlib.sha256(json.dumps([
                    selected_ticket[field] for field in ("service_type", "appointment_time", "address", "customer_name", "phone", "urgency")
                ], ensure_ascii=False).encode()).hexdigest()[:16]
                with st.form(f"ticket_review_form_{selected_id}_{review_version}"):
                    review_labels = {
                        "service_type": "服务类型", "appointment_time": "预约时间", "address": "服务地址",
                        "customer_name": "客户姓名", "phone": "电话", "urgency": "紧急程度",
                    }
                    reviewed_fields = {
                        field: st.text_input(label, value=selected_ticket[field], key=f"review_{selected_id}_{field}_{review_version}")
                        for field, label in review_labels.items()
                    }
                    confirmed = st.checkbox("我已核对客户原始需求与字段", key=f"review_confirmed_{selected_id}_{review_version}")
                    review_submitted = st.form_submit_button("审核通过并保存")
                if review_submitted:
                    if not confirmed:
                        st.warning("请先核对原始需求和字段，并勾选确认。")
                    else:
                        try:
                            reviewed_ticket = review_ticket(selected_id, reviewed_fields)
                        except ValueError as error:
                            st.warning(str(error))
                        except (sqlite3.Error, OSError):
                            st.error("审核保存失败，请检查数据库后重试。")
                        else:
                            if reviewed_ticket:
                                current_ticket = st.session_state.get("latest_ticket")
                                if current_ticket and current_ticket["id"] == selected_id:
                                    st.session_state["latest_ticket"] = reviewed_ticket
                                st.session_state["status_update_message"] = f"人工审核已保存，当前状态为“{reviewed_ticket['status']}”。"
                                st.rerun()
                            else:
                                st.warning("工单不存在或已完成审核，请刷新后重试。")
            else:
                statuses = ("待补充信息", "待确认", "待派单", "服务中", "已完成", "异常", "人工审核")
                with st.form("ticket_status_form"):
                    new_status = st.selectbox(
                        "修改状态",
                        options=statuses,
                        index=statuses.index(selected_ticket["status"]) if selected_ticket["status"] in statuses else None,
                        key=f"target_status_{selected_id}_{selected_ticket['status']}",
                        placeholder="请选择目标状态",
                    )
                    submitted = st.form_submit_button("保存状态")

                if submitted:
                    if new_status is None:
                        st.warning("请选择目标状态。")
                    elif new_status == selected_ticket["status"]:
                        st.info("状态未变化，无需保存。")
                    else:
                        try:
                            updated = change_ticket_status(selected_id, new_status)
                        except ValueError as error:
                            st.warning(str(error))
                        except (sqlite3.Error, OSError):
                            st.error("状态保存失败，请检查数据库后重试。")
                        else:
                            if updated:
                                current_ticket = st.session_state.get("latest_ticket")
                                if current_ticket and current_ticket["id"] == selected_id:
                                    current_ticket["status"] = new_status
                                    current_ticket["review_required"] = new_status == "人工审核"
                                st.session_state["status_update_message"] = f"工单状态已更新为“{new_status}”。"
                                st.rerun()
                            else:
                                st.warning("未找到所选工单，请刷新页面后重试。")
            show_priority_management(selected_ticket)
            show_advice_management(selected_ticket)
            show_staff_matching(selected_ticket)
            show_ticket_history(selected_id)
        else:
            st.info("暂无历史工单，创建后会在这里显示。")
            st.info("当前暂无业务数据，是否加载演示数据")
            st.caption("加载 120 条虚构工单，覆盖最近 30 天；不调用 AI、不覆盖已有工单。")
            if st.button("加载演示数据"):
                try:
                    loaded_count = seed_demo_data()
                except (sqlite3.Error, OSError, ValueError):
                    st.error("演示数据加载失败，本次未保存，请检查数据库后重试。")
                else:
                    st.session_state["status_update_message"] = (
                        f"已加载 {loaded_count} 条模拟业务工单。" if loaded_count else "数据库已有工单，未追加演示数据。"
                    )
                    st.rerun()

    st.caption(
        "当前版本使用模拟业务数据进行 Demo 展示，记录保存在当前环境的 SQLite 中。"
        "由于采用轻量化部署方案，云端环境重启后数据可能重新初始化；正式业务版本可接入持久化数据库。"
    )

    st.subheader("服务人员管理")
    st.caption("查看服务人员资源池，按状态筛选。可用状态为人工维护的信息，技能与区域用于规则推荐；具体服务能力和预约档期仍需核实。")
    if staff_message := st.session_state.pop("staff_message", None):
        st.success(staff_message)
    try:
        staff_records = list_staff()
    except (sqlite3.Error, OSError):
        st.error("人员列表读取失败，请检查数据库后重试。")
    except ValueError:
        st.error("人员技能数据格式不正确，请检查数据。")
    else:
        if staff_records:
            staff_status = st.selectbox(
                "人员状态筛选", options=("全部", *STAFF_STATUS_LABELS),
                format_func=lambda value: STAFF_STATUS_LABELS.get(value, value), key="staff_status_filter",
            )
            visible_staff = [item for item in staff_records if staff_status == "全部" or item["status"] == staff_status]
            if visible_staff:
                st.dataframe([
                    {"人员编号": item["id"], "姓名": item["name"], "技能": "、".join(item["skills"]) or "未填写",
                     "区域": item["service_area"] or "未填写", "状态": STAFF_STATUS_LABELS[item["status"]],
                     "数据来源": "演示数据（demo）" if item["is_demo"] else "业务录入"}
                    for item in visible_staff
                ], hide_index=True, width="stretch")
            else:
                st.info("当前状态下暂无人员。")
            if any(item["is_demo"] for item in staff_records):
                st.info("标记为 demo 的人员均为虚构演示资料，不代表真实员工或实际服务能力。")
        else:
            st.info("暂无服务人员，可加载 3 名虚构演示人员查看资源池。")
            if st.button("加载演示人员"):
                try:
                    loaded_staff = seed_demo_staff()
                except (sqlite3.Error, OSError, ValueError):
                    st.error("演示人员加载失败，本次未保存，请检查数据库后重试。")
                else:
                    st.session_state["staff_message"] = (
                        f"已加载 {loaded_staff} 名虚构演示人员（demo）。" if loaded_staff else "人员表已有数据，未追加演示人员。"
                    )
                    st.rerun()

    st.subheader("运营数据看板")
    dashboard = None
    try:
        dashboard = get_dashboard_stats()
        operational = get_operational_metrics()
    except (sqlite3.Error, OSError):
        st.error("运营看板读取失败，请检查数据库后重试。")
    except ValueError:
        st.error("运营看板生成失败，工单创建时间格式不正确，请检查数据。")
    else:
        st.markdown("**运营核心指标**")
        total_card, new_card, completion_card, automation_card = st.columns(4)
        total_card.metric("工单总数", dashboard["total_tickets"], help="累计工单数量，包含所有历史工单。")
        new_card.metric("今日新增", dashboard["today_new_tickets"])
        completion_card.metric("完成率", f"{operational['completion_rate']:.0%}", help="当前已完成工单数量 / 全部工单数量，空库为 0%。")
        automation_card.metric("AI自动处理率", f"{operational['ai_auto_processing_rate']:.0%}",
                               help="原始解析的关键字段完整且置信度达到 0.7 的工单 / 全部工单；人工补齐低分工单不计为自动通过。")
        success_card, review_card, latency_card = st.columns(3)
        success_rate = dashboard["ai_success_rate"]
        success_card.metric("AI解析成功率", f"{success_rate:.0%}" if success_rate is not None else (
            "暂不可用" if dashboard["ai_metrics_error"] else "暂无真实调用"
        ), help="成功返回并通过本地校验的真实解析次数 / 真实 SDK 尝试次数；模拟解析不计入。")
        review_card.metric("人工审核数量", dashboard["review_required_count"], help="当前仍需要人工审核的工单数量。")
        average_time = dashboard["avg_response_time"]
        latency_card.metric("平均响应时间", f"{average_time:.2f} 秒" if average_time is not None else "暂无测量")
        completed_card, abnormal_card, calls_card = st.columns(3)
        completed_card.metric("累计已完成", dashboard["status_counts"].get("已完成", 0))
        abnormal_card.metric("异常工单", dashboard["status_counts"].get("异常", 0))
        calls_card.metric("AI调用次数", dashboard["ai_call_count"] if dashboard["ai_call_count"] is not None else "暂不可用")
        if dashboard["ai_metrics_error"]:
            st.warning("AI调用统计暂不可用，请检查调用日志；工单统计仍可查看。")
        st.caption("AI调用次数包含成功和失败的真实 SDK 尝试；模拟解析不计入。平均响应时间来自有耗时记录的尝试。")
        st.caption("AI解析成功率衡量调用和格式校验是否成功，不代表字段提取准确率；人工审核数量为当前待审工单。")
        st.caption("完成率按当前状态计算；AI自动处理率是原始解析的规则通过比例，包含模拟数据，不代表真实 AI 调用成功率或准确率。")
        st.caption(f"今日新增按北京时间 {dashboard['date']} 统计；其余指标与分布统计全部工单。")
        st.metric("高优先级未完成工单", dashboard["high_priority_open_count"],
                  help="最终等级为高，且状态不是已完成或已取消；包含人工审核和异常工单。")
        st.markdown("**最终优先级分布**")
        st.table([{"最终优先级": level, "工单数量": count} for level, count in dashboard["priority_counts"].items()])
        st.caption("仅统计运营确认后的最终等级，尚未确认统一显示待分级。AI调用指标仍仅统计需求解析，优先级解释单独记录。")
        st.metric("建议生成数量", dashboard["advice_generated_count"], help="已生成处理建议的工单数；同一工单重复生成只计一次，包含规则和演示建议。")
        st.markdown("**建议状态分布**")
        st.table([{"建议状态": label, "工单数量": count} for label, count in dashboard["advice_decision_counts"].items()])
        st.caption("只统计已有处理建议的工单，按当前评审状态分类；尚未生成建议的工单不计入。Advisor 调用独立记录，不影响解析或优先级调用统计。")
        st.markdown("**信息补全统计**")
        pending_completion, completed_completion, average_completion = st.columns(3)
        pending_completion.metric("待补全工单数量", dashboard["incomplete_ticket_count"])
        completed_completion.metric("已完成补全数量", dashboard["completed_conversation_count"])
        average_fields = dashboard["avg_completed_fields"]
        average_completion.metric("平均补全字段数量", f"{average_fields:.2f}" if average_fields is not None else "暂无补全记录")
        st.caption("待补全按当前五项必需字段统计；已完成限于已开始补全且当前完整的工单。平均值为已跟踪且当前填齐字段数 / 已开始补全工单数，包含人工审核补齐，不代表效率提升。")
        st.markdown("**服务人员资源**")
        staff_total, staff_available, staff_busy = st.columns(3)
        staff_total.metric("服务人员总数", dashboard["staff_total_count"])
        staff_available.metric("可用人员数量", dashboard["staff_available_count"])
        staff_busy.metric("忙碌人员数量", dashboard["staff_busy_count"])
        st.caption(f"按人员当前状态统计，停用人员计入总数。其中 {dashboard['staff_demo_count']} 名为 demo 数据；完成服务且没有其他未结束派单时，忙碌人员恢复可用，停用人员保持停用。")
        st.markdown("**派单统计**")
        dispatch_total, dispatch_confirmed, dispatch_cancelled = st.columns(3)
        dispatch_total.metric("当前派单数量", dashboard["dispatch_total_count"])
        dispatch_confirmed.metric("已确认派单数量", dashboard["dispatch_confirmed_count"])
        dispatch_cancelled.metric("取消派单数量", dashboard["dispatch_cancelled_count"])
        st.caption(f"按派单表统计：总数包含全部六种状态，已确认指等待接单（confirmed），其中 {dashboard['dispatch_demo_count']} 条为 demo 派单。")
        st.markdown("**服务跟踪统计**")
        service_done, service_error, acceptance_time, service_time = st.columns(4)
        service_done.metric("已完成服务数量", dashboard["service_completed_count"])
        service_error.metric("异常服务数量", dashboard["service_exception_count"])
        accept_seconds, service_seconds = dashboard["avg_acceptance_seconds"], dashboard["avg_service_seconds"]
        acceptance_time.metric("平均接单时间", f"{accept_seconds / 60:.2f} 分钟" if accept_seconds is not None else "暂无数据")
        service_time.metric("平均服务时长", f"{service_seconds / 60:.2f} 分钟" if service_seconds is not None else "暂无数据")
        st.caption("服务数量按派单当前状态统计。接单耗时=接单时间−派单时间；服务时长=完成时间−开始时间，仅统计已完成服务。缺失、无效或负时长不参与平均；包含 demo 数据，时间为运营登记时点，不代表实际履约效率。")
        st.metric("可匹配工单数量", dashboard["matchable_ticket_count"] if dashboard["matchable_ticket_count"] is not None else "暂不可用")
        st.caption("当前匹配：上方可用人员数量按 available 状态统计。可匹配工单为未完成／未取消、且至少有一名技能相关未停用候选的工单；包含忙碌、区域未匹配和 demo 候选，不代表可派单数量。统计实时计算，不保存历史推荐。")
        if dashboard["matching_metrics_error"]:
            st.warning(dashboard["matching_metrics_error"])
        if operational["demo_ticket_count"]:
            st.info(f"当前包含 {operational['demo_ticket_count']} 条模拟业务工单，运营比例仅用于演示。")
        if dashboard["total_tickets"]:
            status_chart, service_chart = st.columns(2)
            with status_chart:
                st.markdown("**工单状态分布**")
                st.vega_lite_chart(
                    [{"状态": status, "工单数量": count} for status, count in dashboard["status_counts"].items()],
                    {
                        "mark": {"type": "arc", "innerRadius": 0},
                        "encoding": {
                            "theta": {"field": "工单数量", "type": "quantitative"},
                            "color": {"field": "状态", "type": "nominal"},
                            "tooltip": [{"field": "状态", "type": "nominal"}, {"field": "工单数量", "type": "quantitative"}],
                        },
                        "height": 280,
                    },
                    width="stretch",
                )
            with service_chart:
                st.markdown("**服务类型分布**")
                st.bar_chart(
                    [{"服务类型": service, "工单数量": count} for service, count in dashboard["service_type_counts"].items()],
                    x="服务类型", y="工单数量", horizontal=True, height=280,
                )
            st.markdown("**最近30天工单趋势**")
            st.line_chart(
                [{"日期": item["date"], "新增工单": item["new_tickets"]} for item in operational["daily_trend"]],
                x="日期", y="新增工单", height=260,
            )
            st.caption("含今天在内的最近 30 个北京时间自然日，按工单创建日期统计新增，缺少数据的日期补零。")
        else:
            st.info("暂无工单数据，创建后会在这里显示状态分布和服务类型统计。")

    st.subheader("运营日报")
    try:
        report = generate_daily_report()
    except (sqlite3.Error, OSError):
        st.error("运营日报读取失败，请检查数据库后重试。")
    except ValueError:
        st.error("运营日报生成失败，工单创建时间格式不正确，请检查数据。")
    else:
        today = report["today"]
        st.write(f"今日工单统计：{report['report_date']}（北京时间）")
        new_column, completed_column, pending_column = st.columns(3)
        new_column.metric("新增", today["total_tickets"])
        completed_column.metric("已完成", today["completed"])
        pending_column.metric("待处理", today["pending"])
        st.write(
            f"待补充信息：{today['pending_information']}　"
            f"待确认：{today['pending_confirmation']}　人工审核：{today['pending_review']}　异常：{today['abnormal']}"
        )
        st.caption("统计今日创建工单的当前状态。待处理包含待处理、待补充信息、待确认、待派单、服务中和人工审核。")
        overall = report["overall"]
        st.write(
            f"累计统计：总工单 {overall['total_tickets']}；已完成 {overall['completed']}；"
            f"待补充信息 {overall['pending_information']}；待确认 {overall['pending_confirmation']}；"
            f"人工审核 {overall['pending_review']}；异常 {overall['abnormal']}。"
        )

with home_dashboard:
    show_home_dashboard(dashboard)
