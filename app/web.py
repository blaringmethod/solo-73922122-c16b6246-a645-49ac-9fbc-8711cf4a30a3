"""Flask 路由与页面。"""
from __future__ import annotations

import os

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .csv_utils import ImportError_
from .db import (
    META_RECEIVED_A,
    connect,
    get_meta,
    get_status,
    init_db,
)

DB_PATH = os.environ.get("EXAM_DB_PATH", "/data/exam.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")


def get_conn():
    if "conn" not in g:
        g.conn = connect(DB_PATH)
    return g.conn


@app.teardown_appcontext
def close_conn(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


@app.before_request
def ensure_schema():
    init_db(get_conn())


# ---------------------------------------------------------------- 首页 / 总览
@app.route("/")
def index():
    conn = get_conn()
    status = get_status(conn)
    stats = {
        "cards": conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"],
        "unknown": conn.execute(
            "SELECT COUNT(*) c FROM cards WHERE is_known=0"
        ).fetchone()["c"],
        "mappings": conn.execute("SELECT COUNT(*) c FROM mapping").fetchone()["c"],
        "supply_cells": conn.execute("SELECT COUNT(*) c FROM supply").fetchone()["c"],
        "assigned": conn.execute("SELECT COUNT(*) c FROM assignment").fetchone()["c"],
        "received_a": get_meta(conn, META_RECEIVED_A, "—"),
    }
    audits = conn.execute("SELECT ts, event, detail FROM audit ORDER BY id DESC LIMIT 12").fetchall()
    return render_template("index.html", status=status, stats=stats, audits=audits)


# ---------------------------------------------------------------- 导入
@app.route("/import/answers", methods=["POST"])
def import_answers():
    f = request.files.get("answer_file")
    if not f or not f.filename:
        flash("请先选择作答 CSV 文件", "error")
        return redirect(url_for("index"))
    try:
        info = services.import_answer_csv(get_conn(), f.read())
        flash(f"作答导入成功：{info['cards']} 张卡、{info['answers']} 条作答", "ok")
    except ImportError_ as exc:
        flash(f"作答导入失败（已保留原状态）：{exc}", "error")
    return redirect(url_for("index"))


@app.route("/import/mapping", methods=["POST"])
def import_mapping():
    f = request.files.get("mapping_file")
    if not f or not f.filename:
        flash("请先选择甲乙卷对照表 CSV", "error")
        return redirect(url_for("index"))
    try:
        info = services.import_mapping_csv(get_conn(), f.read())
        flash(f"对照表导入成功：{info['mappings']} 条映射", "ok")
        _flash_revalidation(info)
    except ImportError_ as exc:
        flash(f"对照表导入失败（已保留原状态）：{exc}", "error")
    return redirect(url_for("index"))


@app.route("/import/supply", methods=["POST"])
def import_supply():
    f = request.files.get("supply_file")
    if not f or not f.filename:
        flash("请先选择待供数 CSV", "error")
        return redirect(url_for("index"))
    try:
        info = services.import_supply_csv(get_conn(), f.read())
        flash(f"待供数导入成功：{info['cells']} 个单元", "ok")
        _flash_revalidation(info)
    except ImportError_ as exc:
        flash(f"待供数导入失败（已保留原状态）：{exc}", "error")
    return redirect(url_for("index"))


def _flash_revalidation(info: dict) -> None:
    """数据重新导入后若现用分配失效，按状态给出暂停提示。"""
    reval = info.get("revalidation")
    if not reval:
        return
    if reval["paused"]:
        flash(f"新资料下现用分配无法证明整表（{reval['reason']}），供数已立即暂停", "warn")
    elif reval["status"] == "PAUSED":
        flash("资料已更新；当前仍处于暂停状态，请重新核对后恢复供数", "warn")


@app.route("/received", methods=["POST"])
def received():
    try:
        v = services.set_received_a(get_conn(), request.form.get("received_a", ""))
        flash(f"实收甲卷份数已登记：{v} 份", "ok")
    except ImportError_ as exc:
        flash(f"登记失败（已保留原状态）：{exc}", "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- 核对页
@app.route("/check")
def check_page():
    conn = get_conn()
    status = get_status(conn)
    cards = conn.execute(
        "SELECT c.card_no, c.is_known, c.paper AS raw_paper, a.paper AS assign_paper "
        "FROM cards c LEFT JOIN assignment a ON a.card_no=c.card_no "
        "ORDER BY c.is_known DESC, c.card_no"
    ).fetchall()
    supply = conn.execute("SELECT * FROM supply ORDER BY qid, std_option").fetchall()
    current = services.current_assignment_valid(conn)
    # 供数目标 vs 现用整表实测
    actual = _actual_counts(conn)
    compare = []
    for s in supply:
        cell = (s["qid"], s["std_option"])
        compare.append({"qid": s["qid"], "std_option": s["std_option"],
                        "target": s["cnt"], "actual": actual.get(cell, 0),
                        "ok": actual.get(cell, 0) == s["cnt"]})
    total_a = sum(
        1 for c in cards
        if (c["raw_paper"] if c["is_known"] else c["assign_paper"]) == "A"
    )
    received = get_meta(conn, META_RECEIVED_A)
    return render_template(
        "check.html",
        status=status,
        cards=cards,
        compare=compare,
        current=current,
        total_a=total_a,
        received=received,
        total_cards=len(cards),
    )


def _actual_counts(conn):
    """现用卷型（含 assignment）下整表实测人数。"""
    rows = conn.execute(
        "SELECT am.qid, am.std_option, COUNT(*) c "
        "FROM answers_mapped am GROUP BY am.qid, am.std_option"
    ).fetchall()
    return {(r["qid"], r["std_option"]): r["c"] for r in rows}


@app.route("/check/run", methods=["POST"])
def check_run():
    result = services.save_assignment_and_verify(get_conn(), resume=False)
    if result["feasible"]:
        flash(f"核对通过：找到可行分配，甲卷 {result['total_a']} 份 = 实收 {result['received_a']} 份，全部人数逐单元复现", "ok")
    else:
        flash("核对未通过：" + result["reason"], "error")
    return redirect(url_for("check_page"))


@app.route("/publish", methods=["POST"])
def publish():
    try:
        services.publish(get_conn())
        flash("已发布供数：现用分配可证明整表，供数生效", "ok")
    except ImportError_ as exc:
        flash(f"发布失败：{exc}", "error")
    return redirect(url_for("check_page"))


@app.route("/resume", methods=["POST"])
def resume():
    result = services.save_assignment_and_verify(get_conn(), resume=True)
    if result["feasible"]:
        flash("已恢复供数：找到符合最新资料的可行分配", "ok")
    else:
        flash("无法恢复供数：" + result["reason"], "error")
    return redirect(url_for("check_page"))


# ---------------------------------------------------------------- 更正
@app.route("/correct/paper", methods=["POST"])
def correct_paper():
    card = request.form.get("card_no", "").strip()
    paper = request.form.get("paper", "")
    try:
        r = services.correct_paper(get_conn(), card, paper)
        if r["paused"]:
            flash(f"已更正，但现用分配无法证明整表（{r['reason']}），供数已立即暂停", "warn")
        elif r["status"] == "PAUSED":
            flash("更正已保存；当前仍处于暂停状态，请在核对页重新核对后恢复供数", "warn")
        else:
            flash("更正已保存，现用分配仍能证明整表，继续供数", "ok")
    except ImportError_ as exc:
        flash(f"更正失败（沿用已提交状态）：{exc}", "error")
    return redirect(request.referrer or url_for("index"))


@app.route("/correct/received", methods=["POST"])
def correct_received():
    try:
        r = services.correct_received_a(get_conn(), request.form.get("received_a", ""))
        if r["paused"]:
            flash(f"实收份数已更正，但甲卷总数不再等于实收（{r['reason']}），供数已立即暂停", "warn")
        elif r["status"] == "PAUSED":
            flash("实收份数已更正；当前仍处于暂停状态，请重新核对后恢复供数", "warn")
        else:
            flash("实收份数已更正，现用分配仍然有效", "ok")
    except ImportError_ as exc:
        flash(f"更正失败（沿用已提交状态）：{exc}", "error")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------- 数据查看
@app.route("/data/<kind>")
def data_view(kind):
    conn = get_conn()
    if kind == "raw":
        rows = conn.execute(
            "SELECT r.card_no, c.is_known, c.paper AS raw_paper, r.pos, r.option "
            "FROM answers_raw r JOIN cards c ON c.card_no=r.card_no "
            "ORDER BY r.card_no, r.pos"
        ).fetchall()
        cols = ["卡号", "卷型码(原值)", "题位", "所选项"]
    elif kind == "mapping":
        rows = conn.execute("SELECT paper, pos, option, qid, std_option FROM mapping ORDER BY paper, pos, option").fetchall()
        cols = ["卷型", "题位", "所选项", "题库题号", "标准选项"]
    elif kind == "mapped":
        rows = conn.execute(
            "SELECT am.card_no, c.is_known, COALESCE(a.paper, c.paper) AS paper, am.qid, am.std_option "
            "FROM answers_mapped am JOIN cards c ON c.card_no=am.card_no "
            "LEFT JOIN assignment a ON a.card_no=am.card_no ORDER BY am.card_no, am.qid"
        ).fetchall()
        cols = ["卡号", "生效卷型", "题库题号", "标准选项"]
    else:
        flash("未知数据页", "error")
        return redirect(url_for("index"))
    return render_template("data.html", kind=kind, rows=rows, cols=cols, status=get_status(conn))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
