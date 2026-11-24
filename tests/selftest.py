"""端到端自测：用 Flask 测试客户端跑通导入→核对→发布→更正→暂停→恢复全流程。

运行：python -I tests/selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 本机若无全局 Flask，可用 EXTRA_PYTHONPATH 指定依赖目录（Docker 内不需要）
_extra = os.environ.get("EXTRA_PYTHONPATH")
if _extra:
    for p in _extra.split(os.pathsep):
        sys.path.insert(0, p)

import app.web as web  # noqa: E402
from app import services  # noqa: E402
from app.db import connect, get_meta, get_status  # noqa: E402

SAMPLES = ROOT / "samples"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def upload(client, endpoint: str, field: str, filename: str, extra_form=None):
    data = {field: (open(SAMPLES / filename, "rb"), filename)}
    if extra_form:
        data.update(extra_form)
    return client.post(endpoint, data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="exam-selftest-")
    db_path = os.path.join(tmp, "test.db")
    web.DB_PATH = db_path
    client = web.app.test_client()

    print("[1] 页面可访问")
    rv = client.get("/")
    check("总览页 200", rv.status_code == 200)
    rv = client.get("/check")
    check("核对页 200", rv.status_code == 200)

    print("[2] 导入作答 / 对照表 / 待供数")
    rv = upload(client, "/import/answers", "answer_file", "answers.csv")
    check("作答导入成功", "导入成功" in rv.get_data(as_text=True), rv.get_data(as_text=True)[:200])
    conn = connect(db_path)
    check("10 张卡", conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"] == 10)
    check("2 张卷型缺失", conn.execute(
        "SELECT COUNT(*) c FROM cards WHERE is_known=0").fetchone()["c"] == 2)
    check("30 条原始作答", conn.execute(
        "SELECT COUNT(*) c FROM answers_raw").fetchone()["c"] == 30)
    conn.close()

    rv = upload(client, "/import/mapping", "mapping_file", "mapping.csv")
    check("对照表导入成功", "导入成功" in rv.get_data(as_text=True))
    rv = upload(client, "/import/supply", "supply_file", "supply.csv")
    check("待供数导入成功", "导入成功" in rv.get_data(as_text=True))
    rv = client.post("/received", data={"received_a": "6"}, follow_redirects=True)
    check("实收甲卷登记 6", "已登记" in rv.get_data(as_text=True))

    print("[3] 未登记/数据不全的守卫")
    conn = connect(db_path)
    r = services.find_feasible_assignment(conn)
    check("资料齐全时可求解", r["feasible"], r.get("reason", ""))
    conn.close()

    print("[4] 核对：求解可行分配")
    rv = client.post("/check/run", follow_redirects=True)
    html = rv.get_data(as_text=True)
    check("核对通过提示", "核对通过" in html, html[:200])
    conn = connect(db_path)
    check("状态=VERIFIED", get_status(conn) == "VERIFIED")
    assign = {r["card_no"]: r["paper"] for r in conn.execute(
        "SELECT card_no, paper FROM assignment")}
    check("缺失卡均已分配", set(assign) == {"C009", "C010"}, str(assign))
    check("恰有 1 张缺失卡分到甲", list(assign.values()).count("A") == 1, str(assign))
    check("换算结果 30 行", conn.execute(
        "SELECT COUNT(*) c FROM answers_mapped").fetchone()["c"] == 30)
    check("甲卷总数=实收6", sum(
        1 for r in conn.execute(
            "SELECT c.paper cp, a.paper ap, c.is_known FROM cards c "
            "LEFT JOIN assignment a ON a.card_no=c.card_no")
        if (r["cp"] if r["is_known"] else r["ap"]) == "A") == 6)
    conn.close()

    print("[5] 人数逐单元复现")
    rv = client.get("/check")
    html = rv.get_data(as_text=True)
    check("页面无偏差行", "偏差" not in html)
    check("页面显示全部一致", html.count("✓ 一致") >= 1)

    print("[6] 发布供数")
    rv = client.post("/publish", follow_redirects=True)
    check("发布成功", "已发布供数" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态=SUPPLYING", get_status(conn) == "SUPPLYING")
    conn.close()

    print("[7] 供数中更正（不影响证明 -> 继续供数）")
    # 把一张已知卡改成它本来的卷型，等同无变化，证明应仍成立
    rv = client.post("/correct/paper", data={"card_no": "C001", "paper": "A"},
                     follow_redirects=True)
    check("无害更正后继续供数", "继续供数" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态仍为 SUPPLYING", get_status(conn) == "SUPPLYING")
    conn.close()

    print("[8] 供数中更正卷型（破坏证明 -> 立即暂停）")
    # C006 原为乙，改成甲，现用分配将无法复现人数
    rv = client.post("/correct/paper", data={"card_no": "C006", "paper": "A"},
                     follow_redirects=True)
    html = rv.get_data(as_text=True)
    check("提示暂停", "供数已立即暂停" in html, html[:200])
    conn = connect(db_path)
    check("状态=PAUSED", get_status(conn) == "PAUSED")
    check("失效分配已清空", conn.execute(
        "SELECT COUNT(*) c FROM assignment").fetchone()["c"] == 0)
    conn.close()

    print("[9] 停供后核对：旧资料下找不到可行分配")
    rv = client.post("/check/run", follow_redirects=True)
    check("核对不通过有原因", "核对未通过" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态保持 PAUSED", get_status(conn) == "PAUSED")
    conn.close()

    print("[10] 按最新资料恢复：把 C006 改回乙后重新核对恢复供数")
    rv = client.post("/correct/paper", data={"card_no": "C006", "paper": "B"},
                     follow_redirects=True)
    rv = client.post("/resume", follow_redirects=True)
    check("恢复供数成功", "已恢复供数" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态回到 SUPPLYING", get_status(conn) == "SUPPLYING")
    check("重新生成分配", conn.execute(
        "SELECT COUNT(*) c FROM assignment").fetchone()["c"] == 2)
    conn.close()

    print("[11] 更正实收份数：不匹配即暂停")
    rv = client.post("/correct/received", data={"received_a": "7"},
                     follow_redirects=True)
    check("份数不符触发暂停", "供数已立即暂停" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态=PAUSED", get_status(conn) == "PAUSED")
    # 改成与可行分配一致的 6 份后恢复
    conn.close()
    rv = client.post("/correct/received", data={"received_a": "6"},
                     follow_redirects=True)
    rv = client.post("/resume", follow_redirects=True)
    check("改回后可恢复", "已恢复供数" in rv.get_data(as_text=True))

    print("[12] 篡改人数的待供数表 -> 核对必然不通过")
    conn = connect(db_path)
    before_status = get_status(conn)
    check("[12前置] 当前为供数中", before_status == "SUPPLYING", before_status)
    info = services.import_supply_csv(conn, (SAMPLES / "supply_bad_counts.csv").read_bytes())
    bad = services.find_feasible_assignment(conn)
    check("篡改人数无解", (not bad["feasible"]) and "不存在" in bad["reason"], bad.get("reason", ""))
    check("重新导入坏供数表立即暂停", info["revalidation"]["paused"] is True)
    check("状态=PAUSED", get_status(conn) == "PAUSED")
    check("失效分配已清空", conn.execute(
        "SELECT COUNT(*) c FROM assignment").fetchone()["c"] == 0)
    conn.close()
    # 暂停期间导回正确供数表：失效分配已清空，仍保持暂停，需显式重新核对恢复
    conn = connect(db_path)
    info2 = services.import_supply_csv(conn, (SAMPLES / "supply.csv").read_bytes())
    check("导回正确表后仍保持暂停", info2["revalidation"]["status"] == "PAUSED")
    conn.close()
    rv = client.post("/resume", follow_redirects=True)
    check("显式恢复供数", "已恢复供数" in rv.get_data(as_text=True))
    conn = connect(db_path)
    check("状态回到 SUPPLYING", get_status(conn) == "SUPPLYING")
    conn.close()

    print("[13] 保存失败沿用已提交状态（非法卷型码 / 非法份数）")
    before = get_status(connect(db_path))
    rv = client.post("/correct/paper", data={"card_no": "C001", "paper": "丙"},
                     follow_redirects=True)
    check("非法卷型码被拒", "更正失败" in rv.get_data(as_text=True))
    check("状态未变", get_status(connect(db_path)) == before)
    rv = client.post("/correct/received", data={"received_a": "-3"},
                     follow_redirects=True)
    check("负份数被拒", "更正失败" in rv.get_data(as_text=True))
    check("状态仍未变", get_status(connect(db_path)) == before)

    print("[14] 导入失败的事务回滚（缺表头 CSV 不破坏现有数据）")
    bad_csv = os.path.join(tmp, "bad.csv")
    open(bad_csv, "w", encoding="utf-8").write("foo,bar\n1,2\n")
    conn = connect(db_path)
    cards_before = conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"]
    try:
        services.import_answer_csv(conn, open(bad_csv, "rb").read())
        check("坏 CSV 应抛错", False)
    except services.ImportError_:
        check("坏 CSV 抛 ImportError", True)
    check("卡数据未被清空", conn.execute(
        "SELECT COUNT(*) c FROM cards").fetchone()["c"] == cards_before)
    conn.close()

    print()
    print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
