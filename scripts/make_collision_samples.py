"""生成“同卡同题号冲突 / 换表”演示数据（独立目录 samples/collision/）。

背景：4 张卡、每题三个题位 P1/P2/P3，K1/K2 卷型已识别（甲/乙各一），
U1/U2 卷型码缺失，由核对求解。各卡（题位, 选项）集合刻意错开，使换表 v2
的“同卡两题位换算到同一题号”冲突可以只落在缺码卡的现用推定卷型上。

各卡答案模式（1/X 为所选项）：
  K1(甲): P1=1, P2=1, P3=X
  K2(乙): P1=1, P2=X, P3=X
  U1(缺): P1=1, P2=X, P3=1
  U2(缺): P1=1, P2=1, P3=X   —— 独有组合 (P2=1, P3=X)

- mapping_v1.csv：初始对照表，U1=甲、U2=乙 可复现 supply_v1.csv（实收甲卷 2 份）。
- mapping_v2_collision_unknown.csv：换表后冲突只落在缺码卡的现用推定卷型
  （U1 的甲：P1/P3 同题号；U2 的乙：P1/P2 同题号），已识别卡 K1/K2 不受影响；
  换表即生效并暂停供数，配合 supply_v2.csv 重新核对后恢复（U1=乙、U2=甲）。
- mapping_v3_collision_known.csv：让已识别卡 K1 按甲卷换算出现同卡同题号冲突；
  导入必须被拒绝，原对照表、换算明细、供数状态全部保留。
"""
from __future__ import annotations

import csv
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "samples", "collision")
os.makedirs(OUT, exist_ok=True)

# (卡号, 卷型码 ''=缺失, P1, P2, P3)
CARDS = [
    ("K1", "甲", "1", "1", "X"),
    ("K2", "乙", "1", "X", "X"),
    ("U1", "",  "1", "X", "1"),
    ("U2", "",  "1", "1", "X"),
]

# 初始对照表 v1：甲、乙各自把 P1/P2/P3 映射到互不相同的题库题号（选项全集覆盖）
MAPPING_V1 = [
    ("甲", "P1", "1", "Q1", "A"),
    ("甲", "P2", "1", "Q3", "A"), ("甲", "P2", "X", "Q3", "B"),
    ("甲", "P3", "1", "Q5", "A"), ("甲", "P3", "X", "Q5", "B"),
    ("乙", "P1", "1", "Q2", "A"),
    ("乙", "P2", "1", "Q4", "A"), ("乙", "P2", "X", "Q4", "B"),
    ("乙", "P3", "1", "Q6", "A"), ("乙", "P3", "X", "Q6", "B"),
]

# 换表 v2：冲突仅落在缺码卡的现用推定卷型
#   甲 P3/1 -> Q1：U1（甲推定，独有 P3=1）的 P1/P3 同题号 Q1；
#                 K1、U2 的 P3=X 走另一映射，K2 在乙卷，均不受影响
#   乙 P2/1 -> Q2：U2（乙推定，独有 P2=1 且 P1=1）的 P1/P2 同题号 Q2；
#                 K2 的 P2=X 走 Q4，K1 在甲卷，U1 的 P3=1 不涉及，均不受影响
MAPPING_V2 = [
    ("甲", "P1", "1", "Q1", "A"),
    ("甲", "P2", "1", "Q3", "A"), ("甲", "P2", "X", "Q3", "B"),
    ("甲", "P3", "1", "Q1", "B"), ("甲", "P3", "X", "Q5", "B"),
    ("乙", "P1", "1", "Q2", "A"),
    ("乙", "P2", "1", "Q2", "B"), ("乙", "P2", "X", "Q4", "B"),
    ("乙", "P3", "1", "Q6", "A"), ("乙", "P3", "X", "Q6", "B"),
]

# 换表 v3：让已识别卡 K1（甲，P1=1 且 P3=X）出现同卡同题号，导入必须被拒绝
MAPPING_V3 = [
    ("甲", "P1", "1", "Q1", "A"),
    ("甲", "P2", "1", "Q3", "A"), ("甲", "P2", "X", "Q3", "B"),
    ("甲", "P3", "1", "Q5", "A"), ("甲", "P3", "X", "Q1", "B"),
    ("乙", "P1", "1", "Q2", "A"),
    ("乙", "P2", "1", "Q4", "A"), ("乙", "P2", "X", "Q4", "B"),
    ("乙", "P3", "1", "Q6", "A"), ("乙", "P3", "X", "Q6", "B"),
]

# v1 下可行分配：K1 甲、K2 乙、U1 甲、U2 乙（实收甲卷 2 份）
ASSIGN_V1 = {"U1": "甲", "U2": "乙"}
# v2 下唯一可行分配：U1 乙、U2 甲（甲卷仍为 2 份：K1 + U2）
ASSIGN_V2 = {"U1": "乙", "U2": "甲"}


def write_answers(path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["卡号", "卷型码", "题位", "所选项"])
        for card, paper, p1, p2, p3 in CARDS:
            for pos, opt in (("P1", p1), ("P2", p2), ("P3", p3)):
                w.writerow([card, paper, pos, opt])


def write_mapping(path: str, mapping) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["卷型码", "题位", "所选项", "题号", "标准选项"])
        w.writerows(mapping)


def mapped(mapping, paper: str, pos: str, opt: str) -> tuple[str, str]:
    for p, po, oo, qid, so in mapping:
        if p == paper and po == pos and oo == opt:
            return qid, so
    raise KeyError((paper, pos, opt))


def write_supply(path: str, mapping, assignment: dict[str, str]) -> None:
    totals: dict[tuple[str, str], int] = {}
    for card, raw, p1, p2, p3 in CARDS:
        paper = assignment.get(card, raw)
        for pos, opt in (("P1", p1), ("P2", p2), ("P3", p3)):
            cell = mapped(mapping, paper, pos, opt)
            totals[cell] = totals.get(cell, 0) + 1
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["题号", "标准选项", "人数"])
        for (qid, so), cnt in sorted(totals.items()):
            w.writerow([qid, so, cnt])


def main() -> None:
    write_answers(os.path.join(OUT, "answers.csv"))
    write_mapping(os.path.join(OUT, "mapping_v1.csv"), MAPPING_V1)
    write_mapping(os.path.join(OUT, "mapping_v2_collision_unknown.csv"), MAPPING_V2)
    write_mapping(os.path.join(OUT, "mapping_v3_collision_known.csv"), MAPPING_V3)
    write_supply(os.path.join(OUT, "supply_v1.csv"), MAPPING_V1, ASSIGN_V1)
    write_supply(os.path.join(OUT, "supply_v2.csv"), MAPPING_V2, ASSIGN_V2)
    print("冲突/换表演示样例已生成于", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
