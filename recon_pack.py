#!/usr/bin/env python3
"""按订单行对账：orders.csv / packages.csv / cancels.jsonl -> diffs.csv / daily.csv / summary.txt / notes.txt

设计要点（与数据实际形态对齐）

* packages.csv 一行一"包裹内的一种货"：同一订单行可拆进多个包裹，一个包裹也可装同一订单
  的多行货；拦截回来的件记成 qty 为负的行，ts 为拦截时刻。故"净发出"= 该订单行全部包裹行
  件数带符号相加，不做日期截断。
* cancels.jsonl 是追加式事件流：按文件顺序重放，同一 oid 以最后一条事件为终局状态；
  uncancel 把订单恢复为未取消，照常对账。
* diffs.csv / daily.csv 是分段文件：已经写好的段（11-01..11-05）逐字节保留在文件头部，
  本脚本只在文件末尾追加尚未写出的日期段（11-06..11-09）与更正行；重复运行不追加任何内容，
  文件字节不变（幂等）。
* 更正行：对于已经写出过的下单日，某订单行的终局读数与文件里该行最后一行不一致（或先前
  因 cancelled 未写入、现在有了读数）时，在文件末尾追加一行更正，同一订单行以最后一行为准。
"""

import argparse
import csv
import io
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta

DATE_FMT = "%Y-%m-%d %H:%M:%S"
SLA = timedelta(days=2)
SEGMENT_START = "2026-11-01"
SEGMENT_END = "2026-11-09"

DIFF_HEADER = ["oid", "sku", "owed", "shipped", "diff", "verdict"]
DAILY_HEADER = ["day", "sku", "ordered", "shipped"]
READING_VERDICTS = ("match", "short", "over", "late", "after_cancel")


def parse_ts(s):
    return datetime.strptime(s.strip(), DATE_FMT)


def load_orders(path):
    lines = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames != ["oid", "sku", "qty", "ordered_at"]:
            raise ValueError("orders.csv 表头不符：%r" % (rd.fieldnames,))
        for r in rd:
            key = (r["oid"], r["sku"])
            if key in seen:
                raise ValueError("orders.csv 出现重复订单行：%s %s" % key)
            seen.add(key)
            lines.append(
                {
                    "oid": r["oid"],
                    "sku": r["sku"],
                    "qty": int(r["qty"]),
                    "ordered_ts": parse_ts(r["ordered_at"]),
                    "day": r["ordered_at"][:10],
                }
            )
    if not lines:
        raise ValueError("orders.csv 为空")
    return lines


def load_packages(path, order_keys):
    """返回 (按订单行的净发出, 按订单行的最后一包发出时刻, 按(发出日, sku)的带符号件数)。"""
    shipped = Counter()
    last_out = {}  # 仅 qty>0 的行：拦截行不是"发出"
    shipped_by_day = Counter()
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames != ["pid", "oid", "sku", "qty", "ts"]:
            raise ValueError("packages.csv 表头不符：%r" % (rd.fieldnames,))
        for r in rd:
            key = (r["oid"], r["sku"])
            if key not in order_keys:
                raise ValueError("packages.csv 引用了不存在的订单行：%s %s" % key)
            qty = int(r["qty"])
            ts = parse_ts(r["ts"])
            shipped[key] += qty
            shipped_by_day[(r["ts"][:10], r["sku"])] += qty
            if qty > 0 and (key not in last_out or ts > last_out[key]):
                last_out[key] = ts
    return shipped, last_out, shipped_by_day


def load_terminal_cancel_state(path):
    """按文件顺序重放事件流，返回每个 oid 的终局状态（cancel / uncancel）。"""
    state = {}
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            ev = json.loads(raw)
            try:
                oid, action = ev["oid"], ev["action"]
                parse_ts(ev["at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("cancels.jsonl 第 %d 行不合法：%r" % (lineno, exc))
            if action not in ("cancel", "uncancel"):
                raise ValueError("cancels.jsonl 第 %d 行 action 非法：%r" % (lineno, action))
            state[oid] = action  # 追加顺序即事件顺序，后到覆盖先到
    return state


def compute_readings(orders, shipped, last_out, cancel_state):
    """以 (oid, sku) 为键返回终局读数。"""
    readings = {}
    for o in orders:
        key = (o["oid"], o["sku"])
        owed = o["qty"]
        sh = shipped.get(key, 0)
        if cancel_state.get(o["oid"]) == "cancel":
            verdict = "cancelled" if sh == 0 else "after_cancel"
        elif sh < owed:
            verdict = "short"
        elif sh > owed:
            verdict = "over"
        else:  # 净发出正好等于订单量，再看最后一包是否晚过下单两天
            last = last_out.get(key)
            verdict = "late" if (last is not None and last > o["ordered_ts"] + SLA) else "match"
        readings[key] = {
            "oid": o["oid"],
            "sku": o["sku"],
            "owed": owed,
            "shipped": sh,
            "diff": owed - sh,
            "verdict": verdict,
            "day": o["day"],
        }
    return readings


def read_existing_csv(path, header):
    """读取已有分段文件，返回 (原始行列表 或 None, 数据行列表)。"""
    if not os.path.exists(path):
        return None, []
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.readlines()
    if not raw:
        return None, []
    rd = csv.DictReader(io.StringIO("".join(raw)))
    if rd.fieldnames != header:
        raise ValueError("%s 表头不符：%r" % (path, rd.fieldnames))
    return raw, list(rd)


def diff_row(r):
    return [r["oid"], r["sku"], str(r["owed"]), str(r["shipped"]), str(r["diff"]), r["verdict"]]


def to_csv_text(header, rows):
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    if header is not None:
        wr.writerow(header)
    wr.writerows(rows)
    return buf.getvalue()


def append_text(path, header, rows):
    """把 rows 追加到分段文件末尾；文件不存在时连表头一起新建。"""
    raw, _ = read_existing_csv(path, header)
    if raw is None:
        text = to_csv_text(header, rows)
    else:
        tail = "" if raw[-1].endswith("\n") else "\n"
        text = "".join(raw) + tail + to_csv_text(None, rows)
    atomic_write(path, text)


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".recon-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


NOTES = """对账口径说明（recon_pack.py）
==============================

一、数据模型与净发出
1. orders.csv 一行一订单行，(oid, sku) 唯一；owed 取该行 qty。
2. packages.csv 一行一"包裹内的一种货"，同一订单行可拆进多个包裹，一个包裹也可装同一订单
   的多行货。qty 带符号：正常发出为正，拦截回来的件记为负，ts 为拦截时刻。
3. shipped = 该订单行全部包裹行 qty 的带符号和（含负数拦截行）。不做日期截断，也不因订单
   被取消而剔除。diff = owed - shipped（正数为净发不足，负数为净发超出）。

二、取消的终局归属
1. cancels.jsonl 是追加式事件流，一行一事件（oid, action, at）。按文件顺序重放，同一 oid
   以最后一条事件为终局状态：cancel 为终局已取消，uncancel 为撤销取消、恢复未取消。
2. 终局已取消（cancel）：
   - 净发出不为零 -> after_cancel（此时净发出不会被当成正常发出，只按此档记）；
   - 净发出为零   -> cancelled。
   cancelled 的订单行没有读数：不写进新段，不计入 rows_checked，也不进 summary 的任何一档。
3. 终局为 uncancel：视同从未取消，照常按 match / short / over / late 对账。
4. 追加事件可能落在早已写好的段里、改变其中订单的结论；因此每次对账都重放全流并按终局
   读数核对，而不是沿用旧文件里的结论。

三、其余 verdict
1. short：净发出 < owed。
2. over ：净发出 > owed。
3. match / late：净发出 == owed。以最后一个 qty>0 的包裹为"最后一包"（拦截行不是发出，
   不参与发齐时点判断）；最后一包 ts 晚于下单时刻 + 2 天为 late，否则为 match。
   恰好等于下单后两天的边界算未晚（match）。

四、diffs.csv 的分段与追加
1. 按下单日分段，范围 2026-11-01..2026-11-09。11-01..11-05 是上一次已写好的段，整段
   逐字节保留，不得涂改；本次只在文件末尾追加 11-06..11-09 的段（段内按 orders.csv 顺序，
   即 oid 顺序）。
2. 已写好的段里，若某订单行因追加事件结论变化，则在文件末尾追加更正行；同一订单行以最后
   一行为准，最后一行即该行读数。
3. 具体三种更正情形：
   - 原先写入、终局变为 cancelled（净发出为零）：追加一行 verdict=cancelled。写好的段不能
     删改旧行，只能追加更正；否则该行最后一行仍是旧结论，与终局读数不符。该行不计入
     rows_checked 及各档（cancelled 无读数）。
   - 原先因 cancelled 未写入、终局变为 uncancel 而有读数：追加一行其终局读数，使其补上。
   - 结论档位改变（如 match -> after_cancel）：追加一行新读数。
4. 新段里终局为 cancelled 的订单行不写入（见二.2），因此它们在文件中没有行。

五、daily.csv 的分段
1. 表头 day,sku,ordered,shipped。day 为自然日；ordered 按下单日汇总该 sku 全部订单行件数；
   shipped 按包裹发出日（ts 的日期）汇总该 sku 带符号件数。两列各自跨订单/包裹独立归日。
2. 同样只覆盖 2026-11-01..2026-11-09；已写好的 11-01..11-05 段保留，本次追加 11-06..11-09
   的段，段内按 day,sku 排序。11-10 才发出的包裹不在本范围，故不计入 daily（shipped 汇总
   止于 11-09 的发出日）。
3. daily 只做补段，不产生更正行（已写出日期段不再回改）。

六、summary.txt
1. 共六行，名目与数值以空格分隔，名目依次为 rows_checked match short over late after_cancel。
2. 全部按终局读数计：rows_checked 为有读数的订单行数（即终局非 cancelled 的订单行），其余
   五项为对应 verdict 的订单行数，五项之和等于 rows_checked。
3. 终局 cancelled 的订单行没有读数，不计入以上任何一数。

七、幂等
重复运行：已写出的日期段不再追加；已是最新读数的行不再产生更正行；输出文件字节不变。
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="按订单行对账（增量、幂等）")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="输入/输出目录（默认脚本所在目录）")
    args = ap.parse_args(argv)
    d = args.dir
    p = lambda name: os.path.join(d, name)

    orders = load_orders(p("orders.csv"))
    order_keys = {(o["oid"], o["sku"]) for o in orders}
    shipped, last_out, shipped_by_day = load_packages(p("packages.csv"), order_keys)
    cancel_state = load_terminal_cancel_state(p("cancels.jsonl"))

    unknown = set(cancel_state) - {o["oid"] for o in orders}
    if unknown:
        raise ValueError("cancels.jsonl 引用了不存在的订单：%s" % sorted(unknown)[:5])

    readings = compute_readings(orders, shipped, last_out, cancel_state)
    order_index = {(o["oid"], o["sku"]): i for i, o in enumerate(orders)}
    seg_days = sorted({o["day"] for o in orders
                       if SEGMENT_START <= o["day"] <= SEGMENT_END})

    # ---------------- diffs.csv ----------------
    _, existing = read_existing_csv(p("diffs.csv"), DIFF_HEADER)
    last_row = {}
    emitted_days = set()
    for r in existing:
        key = (r["oid"], r["sku"])
        if key not in readings:
            raise ValueError("diffs.csv 出现订单表中不存在的行：%s %s" % key)
        last_row[key] = r
        emitted_days.add(readings[key]["day"])

    new_rows = []
    # 1) 尚未写出的日期段（整段追加，cancelled 行不写）。行序沿用已写好段的惯例：
    #    即 orders.csv 的原始顺序（oid 顺序），整段连续写出。
    for o in orders:
        if o["day"] not in seg_days or o["day"] in emitted_days:
            continue
        r = readings[(o["oid"], o["sku"])]
        if r["verdict"] == "cancelled":
            continue
        new_rows.append(diff_row(r))

    # 2) 已写出段中结论变化的行 -> 文末更正行（同一订单行以最后一行为准）
    corrections = []
    for key, r in readings.items():
        if r["day"] not in emitted_days:
            continue
        cur = (str(r["owed"]), str(r["shipped"]), str(r["diff"]), r["verdict"])
        prev = last_row.get(key)
        if prev is None:
            if r["verdict"] != "cancelled":  # 原先因 cancelled 未写，现在有读数
                corrections.append((order_index[key], diff_row(r)))
        else:
            old = (prev["owed"], prev["shipped"], prev["diff"], prev["verdict"])
            if old != cur:
                corrections.append((order_index[key], diff_row(r)))
    corrections.sort(key=lambda x: x[0])
    new_rows.extend(row for _, row in corrections)
    append_text(p("diffs.csv"), DIFF_HEADER, new_rows)

    # ---------------- daily.csv ----------------
    _, daily_existing = read_existing_csv(p("daily.csv"), DAILY_HEADER)
    daily_emitted = {r["day"] for r in daily_existing}
    ordered_by_day = Counter()
    for o in orders:
        ordered_by_day[(o["day"], o["sku"])] += o["qty"]

    daily_rows = []
    for day in seg_days:
        if day in daily_emitted:
            continue
        skus = {s for (dd, s) in ordered_by_day if dd == day}
        skus |= {s for (dd, s) in shipped_by_day if dd == day}
        for sku in sorted(skus):
            od = ordered_by_day.get((day, sku), 0)
            sh = shipped_by_day.get((day, sku), 0)
            if od == 0 and sh == 0:
                continue
            daily_rows.append([day, sku, str(od), str(sh)])
    append_text(p("daily.csv"), DAILY_HEADER, daily_rows)

    # ---------------- summary.txt ----------------
    cnt = Counter(r["verdict"] for r in readings.values())
    rows_checked = sum(cnt[v] for v in READING_VERDICTS)
    if rows_checked + cnt["cancelled"] != len(readings) or len(readings) != len(orders):
        raise AssertionError("读数计数不自洽")
    summary_text = "".join(
        "%s %d\n" % (name, val)
        for name, val in [
            ("rows_checked", rows_checked),
            ("match", cnt["match"]),
            ("short", cnt["short"]),
            ("over", cnt["over"]),
            ("late", cnt["late"]),
            ("after_cancel", cnt["after_cancel"]),
        ]
    )
    atomic_write(p("summary.txt"), summary_text)

    # ---------------- notes.txt ----------------
    atomic_write(p("notes.txt"), NOTES)

    print("diffs.csv 追加 %d 行：新段 %d 行，更正 %d 行"
          % (len(new_rows), len(new_rows) - len(corrections), len(corrections)))
    print("daily.csv 追加 %d 行" % len(daily_rows))
    print("summary: rows_checked=%d match=%d short=%d over=%d late=%d after_cancel=%d (cancelled=%d)"
          % (rows_checked, cnt["match"], cnt["short"], cnt["over"], cnt["late"],
             cnt["after_cancel"], cnt["cancelled"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
