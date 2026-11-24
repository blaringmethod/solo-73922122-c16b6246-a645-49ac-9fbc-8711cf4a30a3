"""业务服务：导入、卷型换算、可行分配求解、供数状态机、更正。"""
from __future__ import annotations

from collections import defaultdict

from .csv_utils import ImportError_, normalize_paper, read_csv_rows
from .db import (
    META_RECEIVED_A,
    META_STATUS,
    audit,
    get_meta,
    get_status,
    set_meta,
)

SOLVE_NODE_LIMIT = 300_000


# ---------------------------------------------------------------- 导入
def import_answer_csv(conn, data: bytes) -> dict:
    """导入作答 CSV：卡号、卷型码(可空)、题位、所选项。整库重置为一场新考试。"""
    rows = read_csv_rows(data, ["卡号", "题位", "所选项"], "作答 CSV")
    # 卷型码列允许缺列（全部视为空）
    try:
        wide = read_csv_rows(data, ["卡号", "卷型码", "题位", "所选项"], "作答 CSV")
        paper_by_line = {r["_lineno"]: r["卷型码"] for r in wide}
    except ImportError_:
        paper_by_line = {}

    cards: dict[str, str | None] = {}
    answers: list[tuple[str, str, str]] = []
    seen_ans: set[tuple[str, str]] = set()
    for r in rows:
        card, pos, opt = r["卡号"], r["题位"], r["所选项"]
        if not card:
            raise ImportError_(f"作答 CSV 第 {r['_lineno']} 行卡号为空")
        if not pos:
            raise ImportError_(f"作答 CSV 第 {r['_lineno']} 行题位为空")
        if not opt:
            raise ImportError_(f"卡 {card} 题位 {pos} 的所选项为空")
        paper = normalize_paper(paper_by_line.get(r["_lineno"], ""))
        if card in cards:
            if paper is not None and cards[card] is not None and paper != cards[card]:
                raise ImportError_(
                    f"卡 {card} 出现两个不同卷型码（{cards[card]} 与 {paper}），每张卡整表须共用一个卷型"
                )
            if cards[card] is None:
                cards[card] = paper
        else:
            cards[card] = paper
        if (card, pos) in seen_ans:
            raise ImportError_(f"卡 {card} 题位 {pos} 重复作答")
        seen_ans.add((card, pos))
        answers.append((card, pos, opt))

    if not cards:
        raise ImportError_("作答 CSV 没有任何卡")

    with conn:  # 失败整体回滚，沿用原状态
        conn.executescript(
            "DELETE FROM answers_mapped; DELETE FROM answers_raw; DELETE FROM cards; "
            "DELETE FROM assignment; DELETE FROM supply; DELETE FROM audit; "
            "DELETE FROM mapping;"
        )
        conn.executemany(
            "INSERT INTO cards(card_no, paper, is_known) VALUES(?,?,?)",
            [(c, p, 1 if p else 0) for c, p in sorted(cards.items())],
        )
        conn.executemany("INSERT INTO answers_raw(card_no, pos, option) VALUES(?,?,?)", answers)
        set_meta(conn, META_STATUS, "IMPORTED")
        conn.execute(f"DELETE FROM meta WHERE key='{META_RECEIVED_A}'")
        audit(conn, "导入作答", f"卡号数 {len(cards)}，作答行 {len(answers)}，卷型缺失卡 {sum(1 for p in cards.values() if p is None)}")
    return {"cards": len(cards), "answers": len(answers)}


def import_mapping_csv(conn, data: bytes) -> dict:
    """导入甲乙卷对照表：卷型码、题位、所选项、题号、标准选项。"""
    rows = read_csv_rows(
        data, ["卷型码", "题位", "所选项", "题号", "标准选项"], "甲乙卷对照表"
    )
    records = []
    seen = set()
    for r in rows:
        paper = normalize_paper(r["卷型码"])
        if paper is None:
            raise ImportError_(f"对照表第 {r['_lineno']} 行卷型码不能为空")
        for col in ("题位", "所选项", "题号", "标准选项"):
            if not r[col]:
                raise ImportError_(f"对照表第 {r['_lineno']} 行 {col} 为空")
        key = (paper, r["题位"], r["所选项"])
        if key in seen:
            raise ImportError_(f"对照表存在重复映射：{paper} 卷 题位 {r['题位']} 选项 {r['所选项']}")
        seen.add(key)
        records.append((paper, r["题位"], r["所选项"], r["题号"], r["标准选项"]))

    with conn:
        conn.execute("DELETE FROM mapping")
        conn.executemany(
            "INSERT INTO mapping(paper, pos, option, qid, std_option) VALUES(?,?,?,?,?)",
            records,
        )
        audit(conn, "导入对照表", f"映射条目 {len(records)}")
        # 换算已知卷型的卡；未知卡待分配后换算（同事务，失败整体回滚）
        recompute_mapped(conn)
        reval = _revalidate_after_change(conn, "对照表重新导入")
    return {"mappings": len(records), "revalidation": reval}


def import_supply_csv(conn, data: bytes) -> dict:
    """导入待供数 CSV：题号、标准选项、人数。"""
    rows = read_csv_rows(data, ["题号", "标准选项", "人数"], "待供数 CSV")
    records = []
    seen = set()
    for r in rows:
        if not r["题号"] or not r["标准选项"]:
            raise ImportError_(f"待供数 CSV 第 {r['_lineno']} 行题号或标准选项为空")
        try:
            cnt = int(r["人数"])
        except ValueError as exc:
            raise ImportError_(f"待供数 CSV 第 {r['_lineno']} 行人数不是整数：{r['人数']!r}") from exc
        if cnt < 0:
            raise ImportError_(f"待供数 CSV 第 {r['_lineno']} 行人数为负：{cnt}")
        key = (r["题号"], r["标准选项"])
        if key in seen:
            raise ImportError_(f"待供数 CSV 题号 {r['题号']} 标准选项 {r['标准选项']} 重复")
        seen.add(key)
        records.append((r["题号"], r["标准选项"], cnt))

    with conn:
        conn.execute("DELETE FROM supply")
        conn.executemany(
            "INSERT INTO supply(qid, std_option, cnt) VALUES(?,?,?)", records
        )
        audit(conn, "导入待供数", f"供数单元 {len(records)}，总人数 {sum(x[2] for x in records)}")
        reval = _revalidate_after_change(conn, "待供数表重新导入")
    return {"cells": len(records), "revalidation": reval}


def set_received_a(conn, value_raw: str) -> int:
    """登记实收甲卷份数。"""
    try:
        value = int(str(value_raw).strip())
    except ValueError as exc:
        raise ImportError_(f"实收甲卷份数不是整数：{value_raw!r}") from exc
    if value < 0:
        raise ImportError_("实收甲卷份数不能为负")
    with conn:
        set_meta(conn, META_RECEIVED_A, value)
        audit(conn, "登记实收甲卷", f"实收 {value} 份")
    return value


# ---------------------------------------------------------------- 换算
def _mapping_dict(conn) -> dict[tuple[str, str, str], tuple[str, str]]:
    return {
        (r["paper"], r["pos"], r["option"]): (r["qid"], r["std_option"])
        for r in conn.execute("SELECT paper, pos, option, qid, std_option FROM mapping")
    }


def effective_paper(conn, card: str) -> str | None:
    row = conn.execute("SELECT paper, is_known FROM cards WHERE card_no=?", (card,)).fetchone()
    if row is None:
        return None
    if row["is_known"]:
        return row["paper"]
    a = conn.execute("SELECT paper FROM assignment WHERE card_no=?", (card,)).fetchone()
    return a["paper"] if a else None


def recompute_mapped(conn) -> None:
    """按当前卷型（含 assignment 中对缺失卡的分配）重算题库题号/标准选项。"""
    m = _mapping_dict(conn)
    rows = conn.execute(
        "SELECT c.card_no, c.is_known, c.paper AS cpaper, a.paper AS apaper, r.pos, r.option "
        "FROM cards c JOIN answers_raw r ON r.card_no=c.card_no "
        "LEFT JOIN assignment a ON a.card_no=c.card_no ORDER BY c.card_no, r.pos"
    ).fetchall()
    out = []
    missing = []
    for r in rows:
        paper = r["cpaper"] if r["is_known"] else r["apaper"]
        if paper is None:
            continue  # 缺失卷型且尚未分配，待求解后再算
        hit = m.get((paper, r["pos"], r["option"]))
        if hit is None:
            missing.append(f"{paper}卷/题位{r['pos']}/选项{r['option']}（卡 {r['card_no']}）")
            continue
        out.append((r["card_no"], hit[0], hit[1]))
    if missing:
        raise ImportError_("以下作答在对照表中找不到映射：" + "；".join(missing[:10]))
    conn.execute("DELETE FROM answers_mapped")
    conn.executemany(
        "INSERT INTO answers_mapped(card_no, qid, std_option) VALUES(?,?,?)", out
    )


# ---------------------------------------------------------------- 求解
def _contrib_vector(conn, card: str, paper: str, m: dict) -> dict | None:
    """单卡在某假定卷型下，对各 (题号,标准选项) 单元贡献的计数；映射缺失返回 None。"""
    vec: dict[tuple[str, int], int] = defaultdict(int)
    for r in conn.execute(
        "SELECT pos, option FROM answers_raw WHERE card_no=?", (card,)
    ):
        hit = m.get((paper, r["pos"], r["option"]))
        if hit is None:
            return None
        vec[hit] += 1
    return dict(vec)


def find_feasible_assignment(conn) -> dict:
    """
    求一个同时满足以下全部条件的卷型分配（仅针对卷型码缺失的卡）：
      1) 换算后每个 (题号, 标准选项) 人数 == 待供数 CSV 人数（整表精确复现）；
      2) 甲卷总数（已知甲 + 分配为甲的缺失卡）== 实收甲卷份数。
    返回 {card_no: 'A'/'B'}；无解返回 {"feasible": False, "reason": ...}。
    """
    received_raw = get_meta(conn, META_RECEIVED_A)
    if received_raw is None:
        return {"feasible": False, "reason": "尚未登记实收甲卷份数"}
    received_a = int(received_raw)
    if conn.execute("SELECT COUNT(*) c FROM supply").fetchone()["c"] == 0:
        return {"feasible": False, "reason": "尚未导入待供数 CSV"}
    if conn.execute("SELECT COUNT(*) c FROM mapping").fetchone()["c"] == 0:
        return {"feasible": False, "reason": "尚未导入甲乙卷对照表"}

    m = _mapping_dict(conn)
    target = {
        (r["qid"], r["std_option"]): r["cnt"]
        for r in conn.execute("SELECT qid, std_option, cnt FROM supply")
    }

    # 已知卷型卡的固定贡献
    base: dict[tuple[str, str], int] = defaultdict(int)
    known_a = 0
    for r in conn.execute("SELECT card_no, paper FROM cards WHERE is_known=1"):
        if r["paper"] == "A":
            known_a += 1
        vec = _contrib_vector(conn, r["card_no"], r["paper"], m)
        if vec is None:
            return {"feasible": False, "reason": f"已知{r['paper']}卷卡 {r['card_no']} 存在对照表未覆盖的作答"}
        for cell, n in vec.items():
            base[cell] += n

    unknown = [
        r["card_no"]
        for r in conn.execute("SELECT card_no FROM cards WHERE is_known=0 ORDER BY card_no")
    ]
    need_a = received_a - known_a  # 缺失卡中需要分配为甲的数量
    if not unknown:
        if need_a != 0:
            return {"feasible": False, "reason": f"无缺失卷型卡，但甲卷总数 {known_a} ≠ 实收 {received_a}"}
        return _finalize(conn, {}, base, target, received_a, known_a)

    # 每张缺失卡的两种贡献向量；某卷型映射缺失则该分支不可用
    card_vecs: dict[str, tuple[dict | None, dict | None]] = {}
    for card in unknown:
        va = _contrib_vector(conn, card, "A", m)
        vb = _contrib_vector(conn, card, "B", m)
        if va is None and vb is None:
            return {"feasible": False, "reason": f"卡 {card} 的作答在甲、乙卷对照表里均无映射"}
        card_vecs[card] = (va, vb)

    # 同分组合并：(vecA, vecB) 完全相同的卡一起决定 k 个甲
    groups_map: dict[tuple, list[str]] = defaultdict(list)
    for card in unknown:
        va, vb = card_vecs[card]
        groups_map[(_freeze(va), _freeze(vb))].append(card)
    groups = []
    for (fa, fb), cards in groups_map.items():
        va, vb = (dict(fa) if fa else None), (dict(fb) if fb else None)
        n = len(cards)
        # 组内甲卷数量 k 的硬边界：整组缺甲映射则只能全乙，缺乙映射则只能全甲
        k_min, k_max = (n, n) if vb is None else ((0, 0) if va is None else (0, n))
        diff, low, high = {}, {}, {}
        cells = set(va or ()) | set(vb or ())
        for cell in cells:
            d = (va or {}).get(cell, 0) - (vb or {}).get(cell, 0)
            if d:
                diff[cell] = d
                low[cell] = n * min(d, 0)
                high[cell] = n * max(d, 0)
        groups.append({
            "cards": cards, "n": n, "va": va or {}, "vb": vb or {},
            "diff": diff, "low": low, "high": high,
            "free": not diff, "k_min": k_min, "k_max": k_max,
        })
    # 约束强的组（有差异、人数多）先搜
    groups.sort(key=lambda g: (g["free"], -len(g["diff"]), -g["n"]))

    universe = set(target) | set(base)
    for g in groups:
        universe.update(g["va"])
        universe.update(g["vb"])
    for cell in universe:
        if base.get(cell, 0) > target.get(cell, 0):
            return {"feasible": False,
                    "reason": f"已知卷型卡已使 {cell[0]}/{cell[1]} 人数 {base[cell]} 超过供数目标 {target.get(cell, 0)}"}

    # 搜索起点：每组取允许的最小甲卷数 k_min
    # k_min=0 -> 全组按乙向量；k_min=n -> 全组按甲向量
    cur = dict(base)
    for g in groups:
        start_vec = g["vb"] if g["k_min"] == 0 else g["va"]
        for cell, per_card in start_vec.items():
            cur[cell] = cur.get(cell, 0) + per_card * g["n"]

    # 剩余组相对搜索起点的可调界（k 在 [k_min,k_max] 内）
    remaining_low: dict = defaultdict(int)
    remaining_high: dict = defaultdict(int)
    for g in groups:
        span = g["k_max"] - g["k_min"]
        for cell, d in g["diff"].items():
            if d < 0:
                remaining_low[cell] += span * d
            else:
                remaining_high[cell] += span * d

    nodes = 0

    def dfs(i: int, cur: dict, rem_low: dict, rem_high: dict, a_left: int) -> list[int] | None:
        nonlocal nodes
        nodes += 1
        if nodes > SOLVE_NODE_LIMIT:
            raise RuntimeError("搜索节点超过上限")
        g = groups[i]
        n = g["n"]
        # 甲卷名额约束 + 该组映射硬边界
        k_lo = max(g["k_min"], a_left - _max_a_after(i))
        k_hi = min(g["k_max"], a_left)
        for k in range(k_lo, k_hi + 1):
            nxt = dict(cur)
            for cell, d in g["diff"].items():
                nxt[cell] = nxt.get(cell, 0) + (k - g["k_min"]) * d
            span = g["k_max"] - g["k_min"]
            # 注意必须覆盖整个 universe：未出现的单元界为 0，不能因字典缺键漏算
            nl, nh = {}, {}
            for c in universe:
                d = g["diff"].get(c, 0)
                nl[c] = rem_low.get(c, 0) - min(d, 0) * span
                nh[c] = rem_high.get(c, 0) - max(d, 0) * span
            ok = True
            for cell in universe:
                lo = nxt.get(cell, 0) + nl.get(cell, 0)
                hi = nxt.get(cell, 0) + nh.get(cell, 0)
                t = target.get(cell, 0)
                if lo > t or hi < t:
                    ok = False
                    break
            if not ok:
                continue
            if i + 1 == len(groups):
                if a_left - k == 0 and all(nxt.get(c, 0) == target.get(c, 0) for c in universe):
                    return [k]
                continue
            res = dfs(i + 1, nxt, nl, nh, a_left - k)
            if res is not None:
                return [k] + res
        return None

    def _max_a_after(i: int) -> int:
        return sum(g["k_max"] for g in groups[i + 1:])

    # 甲卷份数范围预判（按组的硬边界汇总）
    max_unknown_a = sum(g["k_max"] for g in groups)
    min_unknown_a = sum(g["k_min"] for g in groups)
    if not (min_unknown_a <= need_a <= max_unknown_a):
        return {"feasible": False,
                "reason": f"缺失卡 {len(unknown)} 张中需 {need_a} 张甲卷，可行范围为 {min_unknown_a}~{max_unknown_a}"}

    try:
        ks = dfs(0, cur, dict(remaining_low), dict(remaining_high), need_a)
    except RuntimeError as exc:
        return {"feasible": False, "reason": f"{exc}，数据规模下未能在限定搜索内判定，请缩小数据量或检查数据"}
    if ks is None:
        return {"feasible": False, "reason": "不存在能同时复现全部人数且满足实收甲卷份数的卷型分配"}

    assignment = {}
    for g, k in zip(groups, ks):
        for card in g["cards"][:k]:
            assignment[card] = "A"
        for card in g["cards"][k:]:
            assignment[card] = "B"
    return _finalize(conn, assignment, base, target, received_a, known_a, save=False)


def _freeze(vec: dict | None):
    return tuple(sorted(vec.items())) if vec is not None else None


def _finalize(conn, assignment: dict, base: dict, target: dict,
              received_a: int, known_a: int, save: bool = False) -> dict:
    """独立复核：用分配整表重算，必须与供数目标逐单元相等、甲卷总数等于实收。"""
    m = _mapping_dict(conn)
    totals: dict[tuple[str, str], int] = defaultdict(int)
    total_a = 0
    for r in conn.execute("SELECT card_no, paper, is_known FROM cards"):
        paper = r["paper"] if r["is_known"] else assignment.get(r["card_no"])
        if paper is None:
            return {"feasible": False, "reason": f"卡 {r['card_no']} 缺少卷型分配"}
        if paper == "A":
            total_a += 1
        vec = _contrib_vector(conn, r["card_no"], paper, m)
        if vec is None:
            return {"feasible": False, "reason": f"卡 {r['card_no']} 在 {paper} 卷下存在无映射作答"}
        for cell, n in vec.items():
            totals[cell] += n
    cells = set(totals) | set(target)
    mismatch = [
        {"cell": c, "target": target.get(c, 0), "actual": totals.get(c, 0)}
        for c in sorted(cells) if totals.get(c, 0) != target.get(c, 0)
    ]
    if total_a != received_a:
        return {"feasible": False, "reason": f"复核：甲卷总数 {total_a} ≠ 实收 {received_a}"}
    if mismatch:
        return {"feasible": False, "reason": "复核：人数无法逐单元复现", "mismatch": mismatch}
    return {"feasible": True, "assignment": assignment, "totals": dict(totals),
            "total_a": total_a, "received_a": received_a}


def current_assignment_valid(conn) -> dict:
    """用现用 assignment 复核整表（更正后判断能否继续供数）。"""
    assignment = {r["card_no"]: r["paper"] for r in conn.execute("SELECT card_no, paper FROM assignment")}
    unknown = conn.execute("SELECT COUNT(*) c FROM cards WHERE is_known=0").fetchone()["c"]
    if unknown and len(assignment) != unknown:
        return {"feasible": False, "reason": "存在未分配卷型的缺失卡"}
    received_raw = get_meta(conn, META_RECEIVED_A)
    if received_raw is None:
        return {"feasible": False, "reason": "尚未登记实收甲卷份数"}
    if conn.execute("SELECT COUNT(*) c FROM supply").fetchone()["c"] == 0:
        return {"feasible": False, "reason": "尚未导入待供数 CSV"}
    base: dict = defaultdict(int)
    known_a = 0
    m = _mapping_dict(conn)
    for r in conn.execute("SELECT card_no, paper FROM cards WHERE is_known=1"):
        if r["paper"] == "A":
            known_a += 1
        vec = _contrib_vector(conn, r["card_no"], r["paper"], m)
        if vec is None:
            return {"feasible": False, "reason": f"卡 {r['card_no']} 存在无映射作答"}
        for cell, n in vec.items():
            base[cell] += n
    target = {(r["qid"], r["std_option"]): r["cnt"]
              for r in conn.execute("SELECT qid, std_option, cnt FROM supply")}
    return _finalize(conn, assignment, base, target, int(received_raw), known_a)


# ---------------------------------------------------------------- 状态机动作
def save_assignment_and_verify(conn, resume: bool = False) -> dict:
    """核对：求解并保存一组可行分配。PAUSED 下 resume=True 则直接恢复供数。"""
    result = find_feasible_assignment(conn)
    if not result["feasible"]:
        return result
    with conn:
        conn.execute("DELETE FROM assignment")
        conn.executemany(
            "INSERT INTO assignment(card_no, paper) VALUES(?,?)",
            sorted(result["assignment"].items()),
        )
        recompute_mapped(conn)
        status = get_status(conn)
        if resume and status == "PAUSED":
            set_meta(conn, META_STATUS, "SUPPLYING")
            audit(conn, "核对恢复供数", f"可行分配 {len(result['assignment'])} 张缺失卡，甲卷 {result['total_a']} 份")
        else:
            set_meta(conn, META_STATUS, "VERIFIED")
            audit(conn, "核对通过", f"保存可行分配，缺失卡 {len(result['assignment'])} 张")
        result["status"] = get_status(conn)
    return result


def publish(conn) -> None:
    """发布供数：仅当现用分配仍能证明整表。"""
    status = get_status(conn)
    if status != "VERIFIED":
        raise ImportError_("只有核对通过（VERIFIED）状态才能发布供数")
    check = current_assignment_valid(conn)
    if not check["feasible"]:
        raise ImportError_("发布失败：" + check["reason"])
    with conn:
        set_meta(conn, META_STATUS, "SUPPLYING")
        audit(conn, "发布供数", f"甲卷 {check['total_a']} 份，供数单元全部复现")


def correct_paper(conn, card: str, new_paper_raw: str) -> dict:
    """更正某张卡的卷型码（可改回空=缺失，重新参与分配）。"""
    row = conn.execute("SELECT card_no, paper, is_known FROM cards WHERE card_no=?", (card,)).fetchone()
    if row is None:
        raise ImportError_(f"卡号 {card} 不存在")
    new_paper = normalize_paper(new_paper_raw)
    old_desc = row["paper"] if row["is_known"] else "空(缺失)"
    with conn:
        conn.execute("UPDATE cards SET paper=?, is_known=? WHERE card_no=?",
                     (new_paper, 1 if new_paper else 0, card))
        conn.execute("DELETE FROM assignment WHERE card_no=?", (card,))
        recompute_mapped(conn)
        return _post_correction(conn, f"更正卡 {card} 卷型 {old_desc} -> {new_paper or '空(缺失)'}")


def correct_received_a(conn, value_raw: str) -> dict:
    """更正实收甲卷份数。"""
    try:
        value = int(str(value_raw).strip())
    except ValueError as exc:
        raise ImportError_("实收甲卷份数不是整数") from exc
    if value < 0:
        raise ImportError_("实收甲卷份数不能为负")
    old = get_meta(conn, META_RECEIVED_A)
    with conn:
        set_meta(conn, META_RECEIVED_A, value)
        return _post_correction(conn, f"更正实收甲卷 {old} -> {value}")


def _revalidate_after_change(conn, detail: str) -> dict:
    """资料变更后的统一复核：现用分配仍能证明整表则维持状态，否则立即暂停。

    供数中（SUPPLYING）失效 -> PAUSED；已暂停（PAUSED）下继续变更则保持暂停，
    必须由教务员在核对页显式“重新核对并恢复供数”；其他状态失效回到 IMPORTED。
    供数表/对照表重新导入与人工更正共用本逻辑。
    """
    status = get_status(conn)
    check = current_assignment_valid(conn)
    if check["feasible"]:
        recompute_mapped(conn)
        if status == "SUPPLYING":
            audit(conn, "变更后复核-继续供数", detail)
        elif status == "PAUSED":
            audit(conn, "暂停中变更-待人工恢复", detail)
        else:
            audit(conn, "变更后复核-分配仍有效", detail)
        return {"changed": True, "paused": False, "status": get_status(conn), "detail": detail}
    # 现用分配失效：清掉失效分配；供数中则立即暂停，其他状态回到待核对
    conn.execute("DELETE FROM assignment")
    conn.execute("DELETE FROM answers_mapped WHERE card_no IN (SELECT card_no FROM cards WHERE is_known=0)")
    if status in ("SUPPLYING", "PAUSED"):
        set_meta(conn, META_STATUS, "PAUSED")
        audit(conn, "变更-暂停供数" if status == "SUPPLYING" else "暂停中变更-仍无法证明",
              f"{detail}；原因：{check['reason']}")
        return {"changed": True, "paused": True,
                "status": "PAUSED", "detail": detail, "reason": check["reason"]}
    set_meta(conn, META_STATUS, "IMPORTED")
    audit(conn, "变更-原分配失效", f"{detail}；原因：{check['reason']}")
    return {"changed": True, "paused": False,
            "status": "IMPORTED", "detail": detail, "reason": check["reason"]}


def _post_correction(conn, detail: str) -> dict:
    """人工更正（卷型码/实收份数）后的复核，规则同 _revalidate_after_change。"""
    return _revalidate_after_change(conn, detail)
