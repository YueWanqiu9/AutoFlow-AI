"""面试演示的展示组件；不修改工单、人员或服务规则。"""

import streamlit as st

from database.seed_demo_flow import DEMO_REQUEST, DemoFlowError, run_demo_flow

FIELD_LABELS = {"service_type": "服务类型", "appointment_time": "预约时间", "address": "服务地址",
                "customer_name": "客户姓名", "phone": "联系电话", "urgency": "紧急程度"}
EVENT_LABELS = {
    "ticket_created": "创建工单", "priority_generated": "生成优先级建议",
    "conversation_started": "开始信息补全", "conversation_question_generated": "提出补充问题",
    "field_filled": "补齐字段", "conversation_completed": "信息补全完成",
    "ticket_reviewed": "人工审核", "status_changed": "工单状态变化",
    "priority_confirmed": "确认优先级", "advice_generated": "生成处理建议",
    "advice_reviewed": "评审处理建议", "dispatch_created": "确认派单",
    "staff_assigned": "人员占用", "dispatch_accepted": "确认接单",
    "service_started": "开始服务", "service_completed": "完成服务",
}


def _show_step(step):
    number, details = step["number"], step["details"]
    st.markdown(f"### {number}. {step['title']}")
    st.write(step["summary"])
    if number == 1:
        for label, field in ((label, field) for field, label in FIELD_LABELS.items()):
            item = details[field]
            st.write(f"**{label}**：{item['value'] or '待补充'} · 模拟置信度 {item['confidence']:.0%}")
        st.caption("预置解析用于展示六字段接口，分数不代表准确率；本流程不调用付费 AI。")
    elif number == 2:
        st.write(f"工单编号：{details['id']}")
        st.warning(f"当前工单状态：{details['status']}")
    elif number == 3:
        for field in details["missing"]:
            st.write(f"• {FIELD_LABELS[field]}")
        st.caption("缺电话影响信息完整性，不会因此降低优先级。")
    elif number == 4:
        for message in details["conversation"]["messages"]:
            role = {"user": "演示客户", "assistant": "补全助手", "system": "系统"}[message["role"]]
            st.write(f"**{role}**：{message['content']}")
        st.success(f"演示运营审核通过 · 当前工单状态：{details['ticket']['status']}")
    elif number == 5:
        st.write(f"规则建议：{details['priority_suggested']} → 人工确认：{details['priority_final']}")
        st.write(details["priority_reason"])
    elif number == 6:
        for key, label in (("missing_information", "待补信息"), ("next_steps", "处理步骤"), ("attention_points", "注意事项")):
            st.markdown(f"**{label}**")
            for index, text in enumerate(details["advice"][key], 1):
                st.write(f"{index}. {text}")
            if not details["advice"][key]:
                st.write("无")
        st.caption("来源：演示规则模板 · 演示运营已采纳，最终方案已保存。")
    elif number == 7:
        candidate = details["candidates"][0]
        st.write(f"**{details['staff']['name']}** · 规则评分 {candidate['score']} / 100")
        for reason in candidate["match_details"]:
            st.write(f"✓ {reason}")
        for risk in details["explanation"]["risk_notes"]:
            st.caption(risk)
        st.caption("本次解释来自规则；AI 不参与评分，也不直接派单。")
    elif number == 8:
        st.success(f"派单编号：{details['dispatch']['id']} · 已确认 · 人员状态：忙碌")
        st.write(f"确认人：{details['dispatch']['confirmed_by']}")
        st.write(f"确认原因：{details['dispatch']['confirm_reason']}")
    else:
        dispatch = details if number == 9 else details["dispatch"]
        for key, label in (("assigned_at", "派单时间"), ("accepted_at", "接单时间"),
                           ("started_at", "开始时间"), ("completed_at", "完成时间")):
            st.write(f"{label}：{dispatch.get(key) or '尚未发生'}")
        if number == 10:
            st.success("服务已完成 · 演示护理员恢复可用")
            st.write(dispatch["completed_note"])
        st.caption("以上为演示执行时点，不是实际服务耗时；未计算效率提升。")


def show_full_demo():
    st.subheader("一键 Demo · 五分钟理解业务闭环")
    st.write(DEMO_REQUEST)
    st.caption("虚构案例 · 独立演示空间 · 全部数据标记为 demo · 无需 API Key。重复运行复用同一案例。")
    st.info("一键演示会模拟客户回复与运营确认；真实工作台仍需运营逐项核对、确认派单和登记服务。")
    if st.button("一键运行完整演示", key="run_full_demo", type="primary"):
        try:
            with st.spinner("正在执行需求到服务完成的十步演示……"):
                result = run_demo_flow()
            st.session_state["full_demo_result"] = result
            st.session_state["full_demo_step"] = 1
        except DemoFlowError as error:
            st.error(str(error))
    result = st.session_state.get("full_demo_result")
    if result is None:
        st.write("**讲解路线**：需求与补全 → 规则与人工决策 → 资源匹配 → 派单与履约 → 数据与追踪")
        return
    st.success("完整演示已执行 10 / 10 步。可拖动下方导航回看各步骤。")
    current = st.select_slider("选择讲解步骤", options=list(range(1, 11)), key="full_demo_step",
                               format_func=lambda value: f"{value}. {result['steps'][value - 1]['title']}")
    st.progress(current / 10, text=f"当前讲解步骤：{current} / 10（执行已完成）")
    for offset in (0, 5):
        for card, step in zip(st.columns(5), result["steps"][offset:offset + 5]):
            with card, st.container(border=True):
                st.caption(f"{'▶' if step['number'] == current else '✅'} {step['number']}. {step['title']}")
    with st.container(border=True):
        _show_step(result["steps"][current - 1])
    with st.expander("查看本次演示的操作记录"):
        st.caption("记录来自本次业务接口执行，按发生顺序展示。")
        for event in result["events"]:
            actor = "演示运营" if event["actor_type"] == "operator" else "系统"
            st.write(f"**{EVENT_LABELS.get(event['event_type'], event['event_type'])}** · {actor} · {event['created_at']}")
            st.caption(event["reason"])


def show_home_dashboard(dashboard):
    st.subheader("运营 Dashboard")
    result = st.session_state.get("full_demo_result")
    if result:
        st.markdown("**本次完整演示**")
        ticket, completed, history = st.columns(3)
        ticket.metric("演示工单", 1)
        completed.metric("演示完成服务", int(result["dispatch"]["status"] == "completed"))
        history.metric("演示操作记录", len(result["events"]))
        st.caption(f"最终优先级：{result['ticket']['priority_final']} · 处理方案：已采纳 · 服务状态：已完成 · 工单人工状态：{result['ticket']['status']}。两类状态独立展示。")
    st.markdown("**运营工作台概览**")
    if dashboard is None:
        st.info("工作台统计暂不可用，可在运营工作台查看具体提示。")
        return
    total, review, done = st.columns(3)
    total.metric("工作台工单", dashboard["total_tickets"])
    review.metric("工作台待审", dashboard["review_required_count"])
    done.metric("工作台完成服务", dashboard["service_completed_count"])
    st.caption("此处仅统计工作台库，可能含先前加载的练习数据；本次完整演示不计入。详细分布和趋势请切换到运营工作台。")


def show_technology_overview():
    st.subheader("技术架构")
    for card, title, text in zip(st.columns(3), ("交互与解析", "规则与决策", "执行与追踪"), (
        "Streamlit 承接客户需求；OpenAI 可解析为统一字段，演示使用模拟结果。",
        "规则检查完整性与匹配条件；运营确认优先级、处理方案和派单。",
        "SQLite 保存业务记录；关键变更与事件同事务，Dashboard 汇总运营数据。",
    )):
        with card, st.container(border=True):
            st.markdown(f"**{title}**")
            st.write(text)
    st.caption("设计原则：AI 辅助决策，规则约束业务，运营保留确认权，全流程可追踪。")
