"""只读项目介绍页面，不调用 AI 或写入工单。"""

import streamlit as st

st.set_page_config(page_title="项目介绍 · AutoFlow AI", page_icon="ℹ️", layout="wide")
st.title("项目介绍")
st.markdown("### AutoFlow AI · 智能服务工单与运营自动化系统")
st.caption("项目负责人：刘宜琛 · 个人AI应用项目 · 方向：AI应用落地 / 运营数字化")
st.markdown('<a href="/" target="_self">返回演示首页，体验业务流程 Demo</a>', unsafe_allow_html=True)

with st.container(border=True):
    st.subheader("业务背景")
    st.write(
        "服务企业收到的是客户的一段描述，运营最终需要的是可以执行、可以跟踪的服务安排。"
        "从需求接收到服务完成，往往需要多次人工整理与协调。"
    )
    for pain in ("信息来源非结构化，关键字段容易遗漏。", "人工判断依赖经验，依据难以复核。",
                 "人员匹配效率低，需要反复核对技能、区域和状态。", "服务过程缺少追踪，异常与责任节点难以定位。"):
        st.write(f"• {pain}")

with st.container(border=True):
    st.subheader("解决方案")
    st.write(
        "AI + 规则 + 人工协同：AI 整理需求并辅助解释；规则检查信息完整性、推荐条件和服务状态流转；"
        "运营人员确认优先级、处理方案与派单。每次关键操作保留记录，形成可追踪的业务闭环。"
    )

st.subheader("技术架构")
st.code("用户需求（用户输入）\n   ↓\nAI解析\n   ↓\n业务规则（规则引擎）\n   ↓\n运营工作台（SQLite 保存工单与事件）\n   ↓\n服务执行（派单 → 接单 → 开始 → 完成／异常）\n   ↓\n数据分析（运营Dashboard）", language=None)
layers = (
    ("页面入口", "Python + Streamlit，接收需求并展示处理过程。"),
    ("AI解析", "模拟模式供离线展示；真实模式使用 OpenAI Structured Outputs。"),
    ("规则与审核", "关键字段低于 0.7 或缺失时进入人工审核，保留原始需求及解析快照。"),
    ("存储与分析", "SQLite 保存业务与事件；工单状态和服务进度独立统计。"),
)
for card, (name, description) in zip(st.columns(4), layers):
    with card, st.container(border=True):
        st.markdown(f"**{name}**")
        st.write(description)

st.subheader("设计原则")
for principle in ("AI 辅助决策，不替代业务判断。", "规则保证业务安全：缺少必要信息不能派单，禁止非法服务状态跳转。",
                  "全流程可追踪：业务变化与操作事件同事务保存。"):
    st.write(f"• {principle}")

st.subheader("五分钟面试演示")
st.write("回到演示首页，点击“体验完整业务流程”。从需求解析、两轮信息补全，到运营确认、护理员推荐、派单与服务完成，拖动步骤导航回看每一个决策。")
st.write("完整演示使用独立数据库，重复运行复用同一案例；全部资料为 demo，客户回复、人工确认和服务执行均为模拟操作。")
st.caption("Demo 使用预置解析和规则模板，不调用真实 AI；模型自评分不代表准确率，演示耗时不代表真实履约效率。真实业务仍需运营确认。")
