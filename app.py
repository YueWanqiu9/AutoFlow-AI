"""AutoFlow AI 的 Streamlit 入口；业务逻辑放在 modules 中。"""

import sqlite3

import streamlit as st

import config
from database.db import get_all_tickets, init_db, update_ticket_status
from database.seed import seed_demo_data
from modules.ai_parser import AIParserError, get_ai_status, parse_customer_request, parse_demo_request
from modules.analytics import get_operational_metrics
from modules.automation import generate_daily_report
from modules.dashboard import get_dashboard_stats
from modules.rules import CRITICAL_FIELDS, check_confidence, get_confidence_score
from modules.tickets import create_ticket, get_saved_parse_result, review_ticket


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

st.set_page_config(page_title="AutoFlow AI", page_icon="🛠️", layout="wide")
st.title("AutoFlow AI")
st.markdown("### 智能服务工单与运营自动化系统")
st.caption("将自然语言需求转为可追踪的服务工单，让客服录入、人工审核和运营分析连成一个流程。")
st.sidebar.info("从左侧“项目介绍”查看业务背景、解决方案与技术架构。")

try:
    init_db()
except (sqlite3.Error, OSError):
    st.error("数据库初始化失败，请检查数据目录的读写权限后重试。")
    st.stop()

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

st.subheader("当前系统状态")
st.info("当前阶段：已支持 AI 解析、工单管理、运营日报与数据看板。")
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
st.caption("自动填入案例并完成模拟解析、规则判断和工单保存；审核由你核对后提交。")
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

        if selected_ticket["review_required"]:
            st.warning("此工单需人工审核，请核对或修正字段后提交。")
            with st.form(f"ticket_review_form_{selected_id}"):
                review_labels = {
                    "service_type": "服务类型", "appointment_time": "预约时间", "address": "服务地址",
                    "customer_name": "客户姓名", "phone": "电话", "urgency": "紧急程度",
                }
                reviewed_fields = {
                    field: st.text_input(label, value=selected_ticket[field], key=f"review_{selected_id}_{field}")
                    for field, label in review_labels.items()
                }
                confirmed = st.checkbox("我已核对客户原始需求与字段", key=f"review_confirmed_{selected_id}")
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
                        updated = update_ticket_status(selected_id, new_status)
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

st.caption("工单已保存在本地数据库，刷新或重启应用后仍可查看历史记录。")

st.subheader("运营数据看板")
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
