"""CSV 读取与卷型码归一化。"""
from __future__ import annotations

import csv
import io

PAPER_ALIASES = {
    "A": "A", "甲": "A",
    "B": "B", "乙": "B",
}


class ImportError_(Exception):
    """导入/保存失败，向页面回显原因（事务回滚后沿用原状态）。"""


def normalize_paper(raw: str | None) -> str | None:
    """甲/A/a->A，乙/B/b->B，空白->None（卷型缺失，待分配）。无法识别抛错。"""
    if raw is None:
        return None
    s = raw.strip().upper()
    if s == "":
        return None
    if s in PAPER_ALIASES:
        return PAPER_ALIASES[s]
    raise ImportError_(f"无法识别的卷型码 {raw!r}（允许：甲/乙、A/B、留空）")


def read_csv_rows(data: bytes, expected_headers: list[str], label: str) -> list[dict[str, str]]:
    """按表头名读取 CSV，自动处理 UTF-8 BOM 与 \r\n。表头不符直接抛错。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError_(f"{label} 不是 UTF-8 编码：{exc}") from exc
    if not text.strip():
        raise ImportError_(f"{label} 内容为空")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() != "" for c in r)]
    if not rows:
        raise ImportError_(f"{label} 没有有效行")
    headers = [h.strip() for h in rows[0]]
    missing = [h for h in expected_headers if h not in headers]
    if missing:
        raise ImportError_(f"{label} 表头缺少列：{', '.join(missing)}；实际表头：{', '.join(headers)}")
    idx = {h: headers.index(h) for h in expected_headers}
    out: list[dict[str, str]] = []
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        rec = {h: row[idx[h]].strip() for h in expected_headers}
        rec["_lineno"] = lineno
        out.append(rec)
    return out
