"""求解器边界测试：无解判定、甲卷份数约束、同构分组与规模性能。"""
import os, sys, time, tempfile, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_extra = os.environ.get("EXTRA_PYTHONPATH")
if _extra:
    for p in _extra.split(os.pathsep):
        sys.path.insert(0, p)

from app.db import connect, init_db, get_status
from app import services

P, F = 0, 0
def check(name, cond, detail=""):
    global P, F
    if cond: P += 1; print("  ✓", name)
    else: F += 1; print("  ✗", name, detail)

def fresh_db():
    d = tempfile.mkdtemp()
    conn = connect(os.path.join(d, "t.db"))
    init_db(conn)
    return conn

def seed(conn, n_cards, unknown, paper_answer_fn):
    """n_cards 张卡、每张 1 题位 P1 选项 1/2；unknown 张卷型缺失。"""
    cards, raw, mapping, supply = [], [], [], []
    # 甲：选项1->Q1/A, 2->Q1/B；乙：选项1->Q2/A, 2->Q2/B
    mapping = [("A","P1","1","Q1","A"), ("A","P1","2","Q1","B"),
               ("B","P1","1","Q2","A"), ("B","P1","2","Q2","B")]
    conn.executemany("INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)", mapping)
    rows_c, rows_a = [], []
    for i in range(n_cards):
        card = f"C{i:04d}"
        opt = paper_answer_fn(i)
        is_unknown = i >= n_cards - unknown
        rows_c.append((card, None if is_unknown else ("A" if i % 2 == 0 else "B"), 0 if is_unknown else 1))
        rows_a.append((card, "P1", opt))
    conn.executemany("INSERT INTO cards(card_no,paper,is_known) VALUES(?,?,?)", rows_c)
    conn.executemany("INSERT INTO answers_raw(card_no,pos,option) VALUES(?,?,?)", rows_a)
    services.set_received_a(conn, "0")

print("[1] 份数约束：缺失卡需甲数量不可能时无解")
conn = fresh_db()
seed(conn, 4, unknown=2, paper_answer_fn=lambda i: "1")
# seed 内已知卡 C0000甲/C0001乙（Q1/A=1、Q2/A=1）；实收甲=1 => 缺失卡 need_a=0 全乙
conn.execute("INSERT INTO supply VALUES('Q1','A',1)")
conn.execute("INSERT INTO supply VALUES('Q2','A',3)")  # 已知1 + 缺失2
services.set_received_a(conn, "1")  # 已知甲1，缺失卡 need_a=0 全乙
r = services.find_feasible_assignment(conn)
check("need_a=0 有解", r["feasible"], r.get("reason",""))
if r["feasible"]:
    check("两缺失卡均分到乙", all(v=="B" for v in r["assignment"].values()))
services.set_received_a(conn, "5")  # 总卡才4张，不可能
r = services.find_feasible_assignment(conn)
check("超出卡总数无解", not r["feasible"] and "范围" in r["reason"], r.get("reason",""))
conn.close()

print("[2] 人数复现：错误目标无解")
conn = fresh_db()
seed(conn, 4, unknown=2, paper_answer_fn=lambda i: "1")
conn.execute("INSERT INTO supply VALUES('Q2','A',3)")  # 全乙实际是4，任何分配都给不出3
services.set_received_a(conn, "2")
r = services.find_feasible_assignment(conn)
check("人数不可复现无解", not r["feasible"], r.get("reason",""))
conn.close()

print("[3] 同构卡分组：20 张同构缺失卡，要求恰 7 张甲")
conn = fresh_db()
seed(conn, 30, unknown=20, paper_answer_fn=lambda i: "1")
# 已知 10 张按奇偶：5甲->Q1/A=5，5乙->Q2/A=5；缺失 20 张取 7甲13乙
conn.execute("INSERT INTO supply VALUES('Q1','A',12)")  # 5 + 7
conn.execute("INSERT INTO supply VALUES('Q2','A',18)")  # 5 + 13
services.set_received_a(conn, "12")  # 已知甲5 + 缺失甲7
r = services.find_feasible_assignment(conn)
check("同构分组求解成功", r["feasible"], r.get("reason",""))
if r["feasible"]:
    check("恰 7 张甲", list(r["assignment"].values()).count("A") == 7)
    check("复核甲卷=12", r["total_a"] == 12)
# 改实收份数为 13（缺 8 甲），人数目标即无法复现
services.set_received_a(conn, "13")
r = services.find_feasible_assignment(conn)
check("份数与人数冲突时无解", not r["feasible"])
conn.close()

print("[4] 两种卷型映射缺失分支处理")
conn = fresh_db()
conn.execute("INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES('A','P1','1','Q1','A')")
conn.execute("INSERT INTO cards VALUES('CX',NULL,0)")
conn.execute("INSERT INTO answers_raw VALUES('CX','P1','1')")
conn.execute("INSERT INTO supply VALUES('Q1','A',1)")
services.set_received_a(conn, "1")
r = services.find_feasible_assignment(conn)
check("乙分支无映射时强制甲且可解", r["feasible"] and r["assignment"].get("CX")=="A", str(r)[:120])
conn.close()

print("[5] 性能：200 张缺失卡、异构作答，毫秒级返回")
import random
random.seed(42)
conn = fresh_db()
answers = {i: random.choice(["1", "2"]) for i in range(240)}
seed(conn, 240, unknown=200, paper_answer_fn=lambda i: answers[i])
# 固定一个可行分配并据此汇总目标人数
truth = {}
target = {}
random.seed(7)
for i in range(240):
    opt = answers[i]
    paper = ("A" if i % 2 == 0 else "B") if i < 40 else random.choice(["A", "B"])
    truth[i] = paper
    qid = "Q1" if paper == "A" else "Q2"
    so = "A" if opt == "1" else "B"
    target[(qid, so)] = target.get((qid, so), 0) + 1
for (qid, so), cnt in target.items():
    conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, so, cnt))
received = sum(1 for p in truth.values() if p == "A")
services.set_received_a(conn, str(received))
t0 = time.time()
r = services.find_feasible_assignment(conn)
dt = time.time() - t0
check(f"200 缺失卡求解 {dt*1000:.0f}ms，在 2 秒内", dt < 2.0)
check("大实例存在可行解并通过独立复核", r["feasible"], r.get("reason", ""))
if r["feasible"]:
    check("解出甲卷总数=实收", r["total_a"] == received)
# 破坏一个单元人数后必须判无解
conn.execute("UPDATE supply SET cnt=cnt+1 WHERE rowid=1")
r2 = services.find_feasible_assignment(conn)
check("篡改后大实例判无解", not r2["feasible"])
conn.close()

print(f"\n求解器测试：通过 {P}，失败 {F}")
sys.exit(1 if F else 0)
