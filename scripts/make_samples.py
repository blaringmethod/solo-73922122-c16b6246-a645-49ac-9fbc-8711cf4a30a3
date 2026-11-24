"""生成演示用 CSV（独立目录 samples/，与程序代码分离）。

设计：10 张卡，8 张卷型码已知（甲5 乙3），2 张缺失（C009/C010）。
实收甲卷 6 份 => 缺失卡中恰有 1 张须分配为甲。
待供数人数按“C009=甲、C010=乙”的分配构造，整表逐单元可精确复现。
另附两个失败用例 CSV，供演示“核对不通过/更正后暂停”。
"""
from __future__ import annotations

import csv
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "samples")
os.makedirs(OUT, exist_ok=True)

# 题位 P1/P2/P3；甲乙卷映射到不同题库题号，选项也做标准化
MAPPING = [
    # 卷型, 题位, 所选项 -> 题号, 标准选项
    ("甲", "P1", "1", "Q1", "A"), ("甲", "P1", "2", "Q1", "B"),
    ("甲", "P1", "3", "Q1", "C"), ("甲", "P1", "4", "Q1", "D"),
    ("乙", "P1", "1", "Q4", "A"), ("乙", "P1", "2", "Q4", "B"),
    ("乙", "P1", "3", "Q4", "C"), ("乙", "P1", "4", "Q4", "D"),
    ("甲", "P2", "1", "Q2", "A"), ("甲", "P2", "2", "Q2", "B"),
    ("乙", "P2", "1", "Q5", "A"), ("乙", "P2", "2", "Q5", "B"),
    ("甲", "P3", "A", "Q3", "A"), ("甲", "P3", "B", "Q3", "B"),
    ("乙", "P3", "A", "Q6", "A"), ("乙", "P3", "B", "Q6", "B"),
]

# 每张卡：(卡号, 卷型码原值 '' 表示缺失, P1, P2, P3)
CARDS = [
    ("C001", "甲", "1", "1", "A"),
    ("C002", "甲", "2", "2", "B"),
    ("C003", "甲", "3", "1", "A"),
    ("C004", "甲", "4", "2", "A"),
    ("C005", "甲", "1", "1", "B"),
    ("C006", "乙", "1", "1", "A"),
    ("C007", "乙", "2", "2", "B"),
    ("C008", "乙", "3", "1", "A"),
    ("C009", "",  "2", "1", "A"),   # 目标分配：甲
    ("C010", "",  "1", "2", "B"),   # 目标分配：乙
]

# 按目标分配（C009 甲、C010 乙）由 write_supply 自动汇总整表人数，
# 保证待供数 CSV 与该可行分配严格一致；另生成一份篡改人数的失败用例。


def write_answers(path: str, cards) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["卡号", "卷型码", "题位", "所选项"])
        for card, paper, p1, p2, p3 in cards:
            for pos, opt in (("P1", p1), ("P2", p2), ("P3", p3)):
                w.writerow([card, paper, pos, opt])


def write_mapping(path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["卷型码", "题位", "所选项", "题号", "标准选项"])
        w.writerows(MAPPING)


def mapped(paper: str, pos: str, opt: str) -> tuple[str, str]:
    for p, po, oo, qid, so in MAPPING:
        if p == paper and po == pos and oo == opt:
            return qid, so
    raise KeyError((paper, pos, opt))


def write_supply(path: str, cards, assignment: dict[str, str]) -> None:
    totals: dict[tuple[str, str], int] = {}
    for card, raw, p1, p2, p3 in cards:
        paper = assignment.get(card, raw)
        for pos, opt in (("P1", p1), ("P2", p2), ("P3", p3)):
            cell = mapped(paper, pos, opt)
            totals[cell] = totals.get(cell, 0) + 1
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["题号", "标准选项", "人数"])
        for (qid, so), cnt in sorted(totals.items()):
            w.writerow([qid, so, cnt])
    return totals


def main() -> None:
    write_answers(os.path.join(OUT, "answers.csv"), CARDS)
    write_mapping(os.path.join(OUT, "mapping.csv"))
    totals = write_supply(
        os.path.join(OUT, "supply.csv"), CARDS, {"C009": "甲", "C010": "乙"}
    )

    # 失败用例 1：人数被篡改（Q1/A 多 1 人），任何分配都无法复现
    bad = dict(totals)
    bad[("Q1", "A")] = bad[("Q1", "A")] + 1
    with open(os.path.join(OUT, "supply_bad_counts.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["题号", "标准选项", "人数"])
        for (qid, so), cnt in sorted(bad.items()):
            w.writerow([qid, so, cnt])

    # 失败用例 2：实收份数对不上时可配合使用（页面直接改份数即可，无需文件）
    print("样例已生成于", os.path.abspath(OUT))
    print("整表目标人数：")
    for cell, cnt in sorted(totals.items()):
        print(" ", cell, cnt)


if __name__ == "__main__":
    main()
