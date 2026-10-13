"""显式加载少量虚构 demo 人员；不自动运行，不伪造历史或真实联系方式。"""

from database import db
from modules.staff import _insert_staff

DEMO_STAFF = (
    {"name": "张护理员（演示）", "skills": ["老人护理", "助浴"], "service_area": "红塔区", "status": "available"},
    {"name": "李护理员（演示）", "skills": ["康复辅助"], "service_area": "玉溪市", "status": "busy"},
    {"name": "王保洁员（演示）", "skills": ["居家保洁"], "service_area": "红塔区", "status": "inactive"},
)


def seed_demo_staff() -> int:
    """仅在人员表为空时整批创建，人员与事件全部成功才提交；重复执行返回 0。"""
    db.init_db()
    with db._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM staff LIMIT 1").fetchone():
            return 0
        for profile in DEMO_STAFF:
            _insert_staff(connection, **profile, is_demo=True, actor_type="system")
    return len(DEMO_STAFF)


if __name__ == "__main__":
    print(f"已加载 {seed_demo_staff()} 名虚构演示人员（demo）。")
