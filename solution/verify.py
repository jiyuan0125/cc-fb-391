#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立校验 align.py 的输出（与 align.py 不同的实现：运单在站集合模型）。"""
import csv
from collections import defaultdict
from datetime import datetime, timedelta

DATA = "/workspace"
OUT = "/workspace/solution/output"
DELTA = {"到件": 1, "离站": -1, "离站撤销": 1}
BOUNDARY_HOUR_TO_SHIFT = {6: "晚班", 14: "早班", 22: "中班"}


def pdt(s):
    return datetime.fromisoformat(s.strip())


with open(f"{DATA}/tracking.csv", encoding="utf-8") as f:
    events = [dict(r, _id=i) for i, r in enumerate(csv.DictReader(f))]
with open(f"{DATA}/shift_snapshots.csv", encoding="utf-8") as f:
    snapshots = list(csv.DictReader(f))
with open(f"{OUT}/alignment.csv", encoding="utf-8") as f:
    align = list(csv.DictReader(f))
align_idx = {(r["网点"], pdt(r["快照时刻"])): r for r in align}

# 期望切点序列（与快照范围一致，每8小时）
snap_times = sorted(pdt(r["快照时刻"]) for r in snapshots)
lo, hi = snap_times[0], snap_times[-1]
boundaries, cur = [], lo
while cur <= hi:
    boundaries.append(cur)
    cur += timedelta(hours=8)
outlets = sorted({r["网点"] for r in events} | {r["网点"] for r in snapshots})

# ---- 模型A：在站运单集合（幂等），逐切点全量重放 ----
truth, window_delta = {}, {}
for o in outlets:
    onsite = set()
    evs = sorted((r for r in events if r["网点"] == o), key=lambda r: r["时间"])
    i, prev_b = 0, None
    for b in boundaries:
        wd = 0
        while i < len(evs) and datetime.fromisoformat(evs[i]["时间"]) <= b:
            r = evs[i]
            if r["事件"] == "到件":
                onsite.add(r["运单号"])
            elif r["事件"] == "离站":
                onsite.discard(r["运单号"])
            else:  # 离站撤销：重新放回
                onsite.add(r["运单号"])
            if prev_b is None or datetime.fromisoformat(r["时间"]) > prev_b:
                wd += DELTA[r["事件"]]
            i += 1
        truth[(o, b)] = len(onsite)
        window_delta[(o, b)] = wd
        prev_b = b

fails = []

# 1) 流水存量 & 本班净变动与对齐表一致
for (o, b), stock in truth.items():
    row = align_idx[(o, b)]
    if int(row["流水存量"]) != stock:
        fails.append(f"存量不符 {o} {b}: 表={row['流水存量']} 集合模型={stock}")
    if int(row["本班净变动"]) != window_delta[(o, b)]:
        fails.append(f"净变动不符 {o} {b}")

# 2) 8条压切点事件：必须计入刚结束班次
for r in events:
    t = datetime.fromisoformat(r["时间"])
    if (t.hour, t.minute) not in ((6, 0), (14, 0), (22, 0)):
        continue
    row = align_idx[(r["网点"], t)]
    if row["班次"] != BOUNDARY_HOUR_TO_SHIFT[t.hour]:
        fails.append(f"切点归属错误 {r['网点']} {r['时间']} -> {row['班次']}")

# 3) 撤销加回：每条撤销运单在撤销后首个切点仍在站
per = defaultdict(list)
for r in events:
    per[(r["运单号"], r["网点"])].append(r)
n_cancel = sum(1 for chain in per.values() for x in chain if x["事件"] == "离站撤销")

def onsite_at(outlet, b):
    s = set()
    for x in events:
        if x["网点"] != outlet or datetime.fromisoformat(x["时间"]) > b:
            continue
        if x["事件"] in ("到件", "离站撤销"):
            s.add(x["运单号"])
        else:
            s.discard(x["运单号"])
    return s

for (wb, o), chain in per.items():
    for x in chain:
        if x["事件"] != "离站撤销":
            continue
        tc = datetime.fromisoformat(x["时间"])
        b = min(z for z in boundaries if z >= tc)
        if wb not in onsite_at(o, b):
            fails.append(f"撤销未加回 {wb} {o} @ {b}")

# 4) 晚班窗口全部跨午夜且时长8小时
for r in align:
    if r["班次"] != "晚班":
        continue
    s, e = pdt(r["班次起始"]), pdt(r["班次结束"])
    if not (s.hour == 22 and e.hour == 6 and e - s == timedelta(hours=8)):
        fails.append(f"晚班窗口错误: {r}")

# 5) 差异分级 & 登记条数
tab = defaultdict(int)
for r in align:
    tab[r["状态"]] += 1
    if r["状态"] == "缺失":
        continue
    d = int(r["差异"])
    if d != int(r["快照包裹数"]) - int(r["流水存量"]):
        fails.append(f"差异计算错误: {r}")
    want = "一致" if d == 0 else ("容差" if abs(d) == 1 else "异常")
    if want != r["状态"]:
        fails.append(f"分级错误: {r}")

for fn, st in [("tolerance_register.csv", "容差"),
               ("exception_register.csv", "异常"),
               ("missing_register.csv", "缺失")]:
    with open(f"{OUT}/{fn}", encoding="utf-8") as f:
        n = sum(1 for _ in csv.DictReader(f))
    if n != tab[st]:
        fails.append(f"{fn} 条数{n} != 对齐表{st}计数{tab[st]}")

# 6) 快照逐条独立对账
snap_map = {(r["网点"], pdt(r["快照时刻"])): int(r["包裹数"]) for r in snapshots}
missing_in_table = 0
for (o, b), sv in snap_map.items():
    row = align_idx[(o, b)]
    if row["快照包裹数"] == "" or int(row["快照包裹数"]) != sv:
        fails.append(f"快照未正确入表 {o} {b}")
for r in align:
    if r["状态"] == "缺失":
        missing_in_table += 1
        if (r["网点"], pdt(r["快照时刻"])) in snap_map:
            fails.append("误报缺失")

print(f"切点/班次实例数: {len(boundaries)}，网点: {len(outlets)}，对齐表行: {len(align)}")
print(f"撤销条数: {n_cancel}（全部验证加回）")
print(f"状态分布: {dict(tab)}，缺失登记: {missing_in_table}")
print(f"晚班实例: {sum(1 for r in align if r['班次']=='晚班')} 个")
if fails:
    print("失败项:")
    for x in fails:
        print(" -", x)
    raise SystemExit(1)
print("全部独立校验通过 ✔")
