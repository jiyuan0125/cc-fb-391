#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐班次对齐：流水重放的真实存量 vs 交接快照。

班次（本地时间）：
  早班 06:00–14:00，中班 14:00–22:00，晚班 22:00–次日 06:00
事件归属窗口统一为 (起, 止]：压在切点上的事件归进刚结束的班次。
  到件 +1，离站 -1，离站撤销 +1（把误录离站真正加回来）。

差异分级：
  快照缺失            -> 缺失登记
  |快照 - 流水| == 1  -> 容差登记
  |快照 - 流水| >= 2  -> 异常登记
  其余为一致。
"""

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

EVENT_DELTA = {
    "到件": 1,
    "离站": -1,
    "离站撤销": 1,
}

# 切点时刻 -> 该切点结束的班次
BOUNDARY_SHIFT = {
    (6, 0): "晚班",    # 06:00 结束的是跨午夜晚班
    (14, 0): "早班",
    (22, 0): "中班",
}
SHIFT_ORDER = {"早班": 0, "中班": 1, "晚班": 2}
STEP = timedelta(hours=8)


def parse_dt(text):
    return datetime.fromisoformat(text.strip())


def shift_name_of(boundary):
    return BOUNDARY_SHIFT[(boundary.hour, boundary.minute)]


def load_tracking(path):
    """返回 {网点: [(datetime, 事件delta, 原始事件名), ...]}，按时间排序。"""
    per_outlet = defaultdict(list)
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, start=2):
            outlet = row["网点"].strip()
            event = row["事件"].strip()
            if event not in EVENT_DELTA:
                raise ValueError(f"tracking.csv 第{lineno}行未知事件: {event!r}")
            per_outlet[outlet].append(
                (parse_dt(row["时间"]), EVENT_DELTA[event], event)
            )
    for outlet in per_outlet:
        per_outlet[outlet].sort(key=lambda x: x[0])
    return per_outlet


def load_snapshots(path):
    """返回 {(网点, 切点时刻): 包裹数}，并校验无重复、切点合法、班次标签一致。"""
    snaps = {}
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, start=2):
            outlet = row["网点"].strip()
            label = row["班次"].strip()
            ts = parse_dt(row["快照时刻"])
            if (ts.hour, ts.minute) not in BOUNDARY_SHIFT:
                raise ValueError(
                    f"shift_snapshots.csv 第{lineno}行快照时刻不压在班次切点上: {ts}"
                )
            if label != shift_name_of(ts):
                raise ValueError(
                    f"shift_snapshots.csv 第{lineno}行班次标签 {label} 与切点 {ts} 不符"
                )
            key = (outlet, ts)
            if key in snaps:
                raise ValueError(f"重复快照: 网点={outlet} 时刻={ts}")
            snaps[key] = int(row["包裹数"])
    return snaps


def enumerate_boundaries(lo, hi):
    """lo 到 hi 之间（含两端）每 8 小时一个班次切点。"""
    out = []
    cur = lo
    while cur <= hi:
        out.append(cur)
        cur += STEP
    return out


def replay(events, boundaries):
    """
    按 (起, 止] 窗口重放。
    返回 {切点: (期末累计存量, 本班净变动)}，并顺带返回负存量告警列表。
    期末存量 = 该网点全部 t <= 切点 的事件累计（含切点当刻事件）。
    """
    result = {}
    balance = 0
    idx = 0
    n = len(events)
    negatives = []
    prev = None
    for b in boundaries:
        window_delta = 0
        while idx < n and events[idx][0] <= b:
            balance += events[idx][1]
            # 本班窗口 = (上一切点, 本切点]
            if prev is None or events[idx][0] > prev:
                window_delta += events[idx][1]
            idx += 1
        if balance < 0:
            negatives.append((b, balance))
        result[b] = (balance, window_delta)
        prev = b
    return result, negatives


def main():
    ap = argparse.ArgumentParser(description="逐班次对齐流水存量与交接快照")
    ap.add_argument("--tracking", default=str(SCRIPT_DIR.parent / "tracking.csv"))
    ap.add_argument("--snapshots", default=str(SCRIPT_DIR.parent / "shift_snapshots.csv"))
    ap.add_argument("--outdir", default=str(SCRIPT_DIR / "output"))
    args = ap.parse_args()

    tracking = load_tracking(args.tracking)
    snapshots = load_snapshots(args.snapshots)

    outlets = sorted(set(tracking) | {k[0] for k in snapshots})
    snap_times = [ts for (_, ts) in snapshots]
    lo, hi = min(snap_times), max(snap_times)
    boundaries = enumerate_boundaries(lo, hi)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    alignment_rows = []
    tolerance, exception, missing = [], [], []
    consumed_snap_keys = set()
    all_negatives = []

    for outlet in outlets:
        events = tracking.get(outlet, [])
        replayed, negatives = replay(events, boundaries)
        all_negatives.extend((outlet, b, bal) for b, bal in negatives)

        for b in boundaries:
            shift = shift_name_of(b)
            true_stock, window_delta = replayed[b]
            key = (outlet, b)
            snap_val = snapshots.get(key)
            if key in snapshots:
                consumed_snap_keys.add(key)

            base = {
                "网点": outlet,
                "班次": shift,
                "班次起始": (b - STEP).strftime("%Y-%m-%d %H:%M:%S"),
                "班次结束": b.strftime("%Y-%m-%d %H:%M:%S"),
                "快照时刻": b.strftime("%Y-%m-%d %H:%M:%S"),
                "本班净变动": window_delta,
                "流水存量": true_stock,
            }

            if snap_val is None:
                status = "缺失"
                row = {**base, "快照包裹数": "", "差异": ""}
                missing.append({
                    "网点": outlet, "班次": shift,
                    "班次起始": base["班次起始"], "班次结束": base["班次结束"],
                    "应拍快照时刻": base["快照时刻"], "流水存量": true_stock,
                })
            else:
                diff = snap_val - true_stock
                row = {**base, "快照包裹数": snap_val, "差异": diff}
                reg = {
                    "网点": outlet, "班次": shift,
                    "班次起始": base["班次起始"], "班次结束": base["班次结束"],
                    "快照时刻": base["快照时刻"],
                    "快照包裹数": snap_val, "流水存量": true_stock,
                    "差异": diff,
                }
                if diff == 0:
                    status = "一致"
                elif abs(diff) == 1:
                    status = "容差"
                    tolerance.append(reg)
                else:
                    status = "异常"
                    exception.append(reg)
            row["状态"] = status
            alignment_rows.append(row)

    # ---- 对账断言：登记条数与对齐表必须对得上 ----
    n_total = len(alignment_rows)
    n_ok = sum(1 for r in alignment_rows if r["状态"] == "一致")
    n_tol = len(tolerance)
    n_exc = len(exception)
    n_mis = len(missing)
    assert n_total == n_ok + n_tol + n_exc + n_mis, "对齐表状态计数不自洽"
    assert consumed_snap_keys == set(snapshots), "存在未被消费或越界的快照"
    assert len(consumed_snap_keys) == len(snapshots), "快照消费条数不符"

    def write(name, fieldnames, rows):
        with open(outdir / name, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

    align_fields = ["网点", "班次", "班次起始", "班次结束", "快照时刻",
                    "快照包裹数", "流水存量", "本班净变动", "差异", "状态"]
    reg_fields = ["网点", "班次", "班次起始", "班次结束", "快照时刻",
                  "快照包裹数", "流水存量", "差异"]
    miss_fields = ["网点", "班次", "班次起始", "班次结束", "应拍快照时刻", "流水存量"]

    write("alignment.csv", align_fields,
          sorted(alignment_rows, key=lambda r: (r["网点"], r["快照时刻"])))
    write("tolerance_register.csv", reg_fields, tolerance)
    write("exception_register.csv", reg_fields, exception)
    write("missing_register.csv", miss_fields, missing)

    print(f"网点数: {len(outlets)}  班次切点: {lo:%Y-%m-%d %H:%M} ~ {hi:%Y-%m-%d %H:%M}")
    print(f"对齐表总行数（网点×班次实例）: {n_total}")
    print(f"  一致: {n_ok}")
    print(f"  容差登记（差1件）: {n_tol}")
    print(f"  异常登记（差≥2件）: {n_exc}")
    print(f"  缺失登记（整班无快照）: {n_mis}")
    print(f"快照消费: {len(consumed_snap_keys)}/{len(snapshots)} 条，全部对上")
    print(f"对账: {n_ok}+{n_tol}+{n_exc}+{n_mis} = {n_total}，与对齐表行数一致")
    if all_negatives:
        print("警告：重放出现负存量:", all_negatives)
    print(f"输出目录: {outdir}")


if __name__ == "__main__":
    main()
