"""只读项目介绍页面，不调用 AI 或写入工单。"""

import streamlit as st

st.set_page_config(page_title="项目介绍 · AutoFlow AI", page_icon="ℹ️", layout="wide")
st.title("项目介绍")
st.markdown("### AutoFlow AI · 智能服务工单与运营自动化系统")

with st.container(border=True):
    st.subheader("业务背景")
    st.write(
        "企业客服收到的服务需求通常以自然语言表达，整理服务类型、预约时间、地址和联系方式等信息仍依赖人工。"
        "信息缺失或录入不一致会增加沟通与后续运营处理的负担。"
    )

with st.container(border=True):
    st.subheader("解决方案")
    st.write(
        "通过 AI 自动解析自然语言需求，生成标准化工单；用业务规则识别缺失信息和低置信度字段，"
        "交由运营人员核对后继续处理。SQLite 保存工单，Dashboard 展示工单与真实 AI 调用指标。"
    )

st.subheader("技术架构")
st.code("用户输入\n   ↓\nAI解析\n   ↓\n规则引擎\n   ↓\nSQLite\n   ↓\n运营Dashboard", language=None)
layers = (
    ("页面入口", "Python + Streamlit，接收需求并展示处理过程。"),
    ("AI解析", "模拟模式供离线展示；真实模式使用 OpenAI Structured Outputs。"),
    ("规则与审核", "关键字段低于 0.7 或缺失时进入人工审核，保留原始需求及解析快照。"),
    ("存储与分析", "SQLite 持久化，按需计算工单统计与真实 AI 调用指标。"),
)
for card, (name, description) in zip(st.columns(4), layers):
    with card, st.container(border=True):
        st.markdown(f"**{name}**")
        st.write(description)

st.subheader("展示重点")
st.write("回到首页点击“一键演示”，观察五步处理过程，再补齐电话完成人工审核，并查看 Dashboard 的变化。")
st.caption("当前是 MVP 展示版本。Demo 分数为预设数据，真实模型自评分也不代表准确率；本项目尚未提供实际运营效率提升的量化评估。")
