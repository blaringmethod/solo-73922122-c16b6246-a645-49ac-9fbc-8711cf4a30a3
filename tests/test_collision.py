"""同卡同题号冲突与换表规则测试（对应本次修复）。

运行：python -I tests/test_collision.py
覆盖：
  1. 同卡两题位按某卷型落到同一题号（选项相同也一样）-> 该卷型不可用，求解排除；
  2. 不同卡各占一个题位落到同一题号 -> 正常换算；
  3. 换表时已识别卷型卡冲突 -> 整事务回滚，保留原对照表、换算明细、供数状态，报卡号题号；
  4. 供数中换表，冲突仅在缺码卡现用推定卷型 -> 新表生效且立即暂停，按新表找到合法分配才恢复；
  5. 网页保存路径不再抛 IntegrityError（保存失败保留已提交结果）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_extra = os.environ.get("EXTRA_PYTHONPATH")
if _extra:
    for p in _extra.split(os.pathsep):
        sys.path.insert(0, p)

from app.db import connect, get_status, init_db  # noqa: E402
from app import services  # noqa: E402
from app.csv_utils import ImportError_  # noqa: E402

SAMPLES = ROOT / "samples" / "collision"
P, F = 0, 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print("  ✓", name)
    else:
        F += 1
        print("  ✗", name, detail)


def fresh():
    d = tempfile.mkdtemp()
    conn = connect(os.path.join(d, "t.db"))
    init_db(conn)
    return conn


def load(conn, answers="answers.csv", mapping="mapping_v1.csv",
         supply="supply_v1.csv", received="2"):
    services.import_answer_csv(conn, (SAMPLES / answers).read_bytes())
    services.import_mapping_csv(conn, (SAMPLES / mapping).read_bytes())
    services.import_supply_csv(conn, (SAMPLES / supply).read_bytes())
    services.set_received_a(conn, received)


# ---------------------------------------------------------------- 1. 求解器排除冲突卷型
print("[1] 同卡同题号冲突：求解器排除冲突分支并保存成功")
conn = fresh()
load(conn)
r = services.find_feasible_assignment(conn)
check("v1 数据可求解", r["feasible"], r.get("reason", ""))
if r["feasible"]:
    check("v1 解为 U1=甲 U2=乙", r["assignment"] == {"U1": "A", "U2": "B"}, str(r["assignment"]))
saved = services.save_assignment_and_verify(conn)
check("保存可行分配不报错", saved["feasible"] and get_status(conn) == "VERIFIED")
check("换算明细 12 行", conn.execute("SELECT COUNT(*) c FROM answers_mapped").fetchone()["c"] == 12)
avail = services.paper_availability(conn)
check("v1 下各卡甲乙均可用", all(v["A"] is None and v["B"] is None for v in avail.values()))
conn.close()

# ---------------------------------------------------------------- 2. 选项相同也判冲突
print("[2] 标准选项相同的同卡同题号仍然冲突")
conn = fresh()
conn.executemany(
    "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)",
    [("A", "P1", "1", "Q1", "A"), ("A", "P2", "1", "Q1", "A"),  # 同题号同选项
     ("B", "P1", "1", "Q2", "A"), ("B", "P2", "1", "Q3", "A")],
)
conn.execute("INSERT INTO cards VALUES('U1',NULL,0)")
conn.executemany("INSERT INTO answers_raw VALUES(?,?,?)",
                 [("U1", "P1", "1"), ("U1", "P2", "1")])
conn.executemany("INSERT INTO supply VALUES(?,?,?)", [("Q2", "A", 1), ("Q3", "A", 1)])
services.set_received_a(conn, "0")
r = services.find_feasible_assignment(conn)
check("甲分支被排除、取乙", r["feasible"] and r["assignment"]["U1"] == "B", r.get("reason", ""))
conn.close()

# ---------------------------------------------------------------- 3. 跨卡同题位正常
print("[3] 不同卡各答一个题位、换算到同一题号：正常")
conn = fresh()
conn.executemany(
    "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)",
    [("A", "P1", "1", "Q1", "A"), ("A", "P2", "1", "Q1", "A")],
)
conn.executemany("INSERT INTO cards(card_no,paper,is_known) VALUES(?,?,?)",
                 [("K1", "A", 1), ("K2", "A", 1)])
conn.executemany("INSERT INTO answers_raw VALUES(?,?,?)",
                 [("K1", "P1", "1"), ("K2", "P2", "1")])
conn.execute("INSERT INTO supply VALUES('Q1','A',2)")
services.set_received_a(conn, "2")
r = services.find_feasible_assignment(conn)
check("跨卡同题号可解", r["feasible"], r.get("reason", ""))
check("跨卡保存成功", services.save_assignment_and_verify(conn)["feasible"])
check("两张卡各保留一行换算明细",
      conn.execute("SELECT COUNT(*) c FROM answers_mapped").fetchone()["c"] == 2)
conn.close()

# ---------------------------------------------------------------- 4. 换表冲突只在缺码卡：暂停->恢复
print("[4] 供数中换表：缺码卡推定卷型冲突 -> 新表生效并暂停，重新核对恢复")
conn = fresh()
load(conn)
services.save_assignment_and_verify(conn)
services.publish(conn)
check("前置状态 SUPPLYING", get_status(conn) == "SUPPLYING")
info = services.import_mapping_csv(
    conn, (SAMPLES / "mapping_v2_collision_unknown.csv").read_bytes()
)
check("换表成功（冲突仅在缺码卡）", info["mappings"] == 10)
check("立即暂停", info["revalidation"]["paused"] and get_status(conn) == "PAUSED")
check("失效分配已清空", conn.execute("SELECT COUNT(*) c FROM assignment").fetchone()["c"] == 0)
check("新对照表已生效",
      conn.execute("SELECT qid FROM mapping WHERE paper='A' AND pos='P3' AND option='1'").fetchone()["qid"] == "Q1")
avail = services.paper_availability(conn)
check("K1/K2 在原码下仍可用", avail["K1"]["A"] is None and avail["K2"]["B"] is None)
check("U1 甲分支冲突、乙可用",
      avail["U1"]["A"] is not None and avail["U1"]["A"][0] == "collision" and avail["U1"]["B"] is None)
check("U2 乙分支冲突、甲可用",
      avail["U2"]["B"] is not None and avail["U2"]["B"][0] == "collision" and avail["U2"]["A"] is None)
# 旧供数目标下已无合法分配（新表改变了单元结构）
check("暂停状态下普通核对不放行", not services.find_feasible_assignment(conn)["feasible"])
# 导入与新表匹配的供数目标，重新核对并恢复
services.import_supply_csv(conn, (SAMPLES / "supply_v2.csv").read_bytes())
r = services.save_assignment_and_verify(conn, resume=True)
check("按新表找到合法分配并恢复", r["feasible"] and get_status(conn) == "SUPPLYING", r.get("reason", ""))
if r["feasible"]:
    check("恢复解为 U1=乙 U2=甲", r["assignment"] == {"U1": "B", "U2": "A"}, str(r["assignment"]))
    check("甲卷总数仍=实收2", r["total_a"] == 2)
conn.close()

# ---------------------------------------------------------------- 5. 换表冲突涉及已识别卡：整体回滚
print("[5] 换表：已识别卷型卡冲突 -> 拒绝并保留原对照表/明细/状态")
conn = fresh()
load(conn)
services.save_assignment_and_verify(conn)
services.publish(conn)
map_before = [tuple(r) for r in conn.execute(
    "SELECT paper,pos,option,qid,std_option FROM mapping ORDER BY paper,pos,option")]
mapped_before = conn.execute("SELECT COUNT(*) c FROM answers_mapped").fetchone()["c"]
status_before = get_status(conn)
try:
    services.import_mapping_csv(
        conn, (SAMPLES / "mapping_v3_collision_known.csv").read_bytes()
    )
    check("v3 导入应被拒绝", False)
except ImportError_ as exc:
    msg = str(exc)
    check("报错含卡号 K1 与题号 Q1", "K1" in msg and "Q1" in msg, msg)
check("状态保留 SUPPLYING", get_status(conn) == status_before)
check("原对照表完整保留", [tuple(r) for r in conn.execute(
    "SELECT paper,pos,option,qid,std_option FROM mapping ORDER BY paper,pos,option")] == map_before)
check("换算明细保留", conn.execute("SELECT COUNT(*) c FROM answers_mapped").fetchone()["c"] == mapped_before)
check("分配保留", conn.execute("SELECT COUNT(*) c FROM assignment").fetchone()["c"] == 2)
conn.close()

# ---------------------------------------------------------------- 6. 网页保存路径不炸 500
print("[6] Flask 路由：冲突数据下核对不抛异常，页面回显原因且状态不变")
import app.web as web  # noqa: E402

d = tempfile.mkdtemp()
web.DB_PATH = os.path.join(d, "web.db")
client = web.app.test_client()
with open(SAMPLES / "answers.csv", "rb") as f:
    client.post("/import/answers", data={"answer_file": (f, "answers.csv")},
                content_type="multipart/form-data")
with open(SAMPLES / "mapping_v2_collision_unknown.csv", "rb") as f:
    client.post("/import/mapping", data={"mapping_file": (f, "m.csv")},
                content_type="multipart/form-data")
with open(SAMPLES / "supply_v2.csv", "rb") as f:
    client.post("/import/supply", data={"supply_file": (f, "s.csv")},
                content_type="multipart/form-data")
client.post("/received", data={"received_a": "2"})
rv = client.post("/check/run", follow_redirects=True)
check("冲突数据下核对页 200（无 500）", rv.status_code == 200)
html = rv.get_data(as_text=True)
check("页面回显不可用标记", "不可用" in html and "同卡同题号" in html)
rv2 = client.post("/check/run", follow_redirects=True)
check("再次核对仍 200，保存失败保留已提交结果", rv2.status_code == 200)

print(f"\n冲突/换表测试：通过 {P}，失败 {F}")
sys.exit(1 if F else 0)
