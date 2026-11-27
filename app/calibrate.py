"""工单校订：用已确认故障的维修工单实测读数，校订故障表中该故障的预期读数。

规则
----
- 只校订当前表里**该故障**那一列的预期读数；每格只能取「原值」或
  「工单中该项检查出现过的值」。按排查规则等价（canonical_reading）的
  读数视为同一个值，不产生新取值。
- 每份工单最多有**一项**与校订后的表不符（空项不计；工单未填的检查
  不参与计数）。
- 原本能用全表区分的故障对，校订后仍须能区分；原本仅靠重启后检查
  就能区分的故障对，校订后仍须仅靠重启后检查就能区分。
- 在满足条件的方案中取**改格数最少**的；最少方案唯一时才给出校订表。

搜索：检查 ≤ 10 格、工单 ≤ 8 份，按改格数 k 迭代加深（IDDFS）+
分支限界（每单不符计数、强制赋值传播、改动数下界），找到 2 个同层
方案即停（用于判定「并列」）。
"""

from __future__ import annotations

from .engine import canonical_reading, clean_reading, PHASE_POST

MAX_ORDERS = 8
# 防御性上限：理论上 k 不会超过「有工单不同读数的格子数」（≤10）
_MAX_ITERS = 4_000_000

_READINGS_KEYS = ("readings", "records", "values", "results", "measurements",
                  "读数", "实测", "实测读数", "检查结果", "记录")
_ID_KEYS = ("id", "order_id", "work_order", "work_order_id", "no", "number",
            "编号", "工单号", "单号")
_FAULT_KEYS = ("fault", "fault_id", "confirmed_fault", "故障", "故障编号", "确认故障")


class CalibrateInputError(ValueError):
    """工单/校订请求不合法；errors 为逐条中文说明。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------- 工单解析

def parse_orders(raw_orders, table):
    """解析工单数组 -> [{id, readings:{ci:canonical}, display:{ci:str}}]。

    - 每份工单为对象：优先取 readings/读数 等键下的 {检查编号: 读数}；
      兼容把检查编号直接写在工单对象上的扁平写法。
    - 空项（null/空白）不计；引用不存在的检查编号报错。
    """
    cid_to_ci = {c["id"]: i for i, c in enumerate(table["checks"])}
    errors = []
    if not isinstance(raw_orders, list) or not raw_orders:
        raise CalibrateInputError([f"orders/工单 必须是 1~{MAX_ORDERS} 份的非空数组"])
    if len(raw_orders) > MAX_ORDERS:
        raise CalibrateInputError([f"工单最多 {MAX_ORDERS} 份，当前收到 {len(raw_orders)} 份"])

    orders = []
    for i, raw in enumerate(raw_orders):
        label = f"第 {i + 1} 份工单"
        if not isinstance(raw, dict):
            errors.append(f"{label}：必须是 JSON 对象")
            continue
        oid = None
        for k in _ID_KEYS:
            if raw.get(k) is not None and str(raw.get(k)).strip():
                oid = str(raw[k]).strip()
                break
        if oid is None:
            oid = f"WO-{i + 1}"
        label = f"{label}（{oid}）"

        robj = None
        for k in _READINGS_KEYS:
            if k in raw:
                if isinstance(raw[k], dict):
                    robj = raw[k]
                else:
                    errors.append(f"{label}：{k} 必须是 {{检查编号: 读数}} 对象")
                    robj = {}
                break
        if robj is None:
            # 扁平兼容：工单对象上直接出现检查编号键
            robj = {k: v for k, v in raw.items() if str(k).strip() in cid_to_ci}

        readings, display = {}, {}
        unknown = []
        for cid, val in robj.items():
            cid_s = str(cid).strip()
            if cid_s not in cid_to_ci:
                unknown.append(cid_s)
                continue
            if val is None or str(val).strip() == "":
                continue  # 空项不计
            canon = canonical_reading(val)
            if not canon:
                continue
            ci = cid_to_ci[cid_s]
            readings[ci] = canon
            display[ci] = clean_reading(val)
        if unknown:
            errors.append(f"{label}：引用了故障表中不存在的检查编号：{', '.join(sorted(unknown))}")
        orders.append({"id": oid, "readings": readings, "display": display})

    if errors:
        raise CalibrateInputError(errors)
    return orders


def extract_fault_id(body, table):
    """从请求体取确认的故障 id（兼容中文键名）。"""
    fid = None
    if isinstance(body, dict):
        for k in _FAULT_KEYS:
            if body.get(k) is not None and str(body.get(k)).strip():
                fid = str(body[k]).strip()
                break
    if not fid:
        raise CalibrateInputError(["缺少已确认故障编号（fault/故障）"])
    if fid not in {f["id"] for f in table["faults"]}:
        raise CalibrateInputError([f"故障 {fid} 不在当前故障表中"])
    return fid


# ---------------------------------------------------------------- 校订主流程

def calibrate(table, fault_id, raw_orders):
    """对 table 中 fault_id 列做工单校订。返回结果字典（见 __init__ 路由）。

    status:
      - no_change_needed：原表已符合（每单不符 ≤1），无需校订
      - unique：有改动且最少改格方案唯一，附 changed_cells 与校订后 table
      - tied：最少改格方案不止一个，暂不出表
      - no_solution：不存在满足全部约束的校订方案
    """
    orders = parse_orders(raw_orders, table)
    faults = table["faults"]
    checks = table["checks"]
    n = len(checks)
    ti = next(i for i, f in enumerate(faults) if f["id"] == fault_id)

    # 原列（比较键 + 展示值）
    orig = [canonical_reading(checks[ci]["expected"][fault_id]) for ci in range(n)]
    orig_display = [checks[ci]["expected"][fault_id] for ci in range(n)]

    # 每格候选值：原值 + 工单中该项出现过的值（canonical 去重，保序）
    cand_values, cand_display = [], []
    for ci in range(n):
        vals = {orig[ci]: orig_display[ci]}
        for o in orders:
            if ci in o["readings"]:
                vals.setdefault(o["readings"][ci], o["display"][ci])
        cand_values.append(list(vals.keys()))
        cand_display.append(vals)

    # 每份工单有读数的格子（不符计数只在这些格子上发生）
    order_cells = [sorted(o["readings"].keys()) for o in orders]
    order_value = [o["readings"] for o in orders]
    # 每格涉及哪些工单
    cell_orders = [[] for _ in range(n)]
    for wi, cells in enumerate(order_cells):
        for ci in cells:
            cell_orders[ci].append(wi)

    # ---- 可区分性约束（基于原表计算，校订后须保持）----
    exp_all = [
        [canonical_reading(checks[ci]["expected"][f["id"]]) for ci in range(n)]
        for f in faults
    ]
    post_cis = [ci for ci in range(n) if checks[ci]["phase"] == PHASE_POST]
    sep_req = []  # (gi, need_full, need_post)
    for gi in range(len(faults)):
        if gi == ti:
            continue
        need_full = any(exp_all[ti][ci] != exp_all[gi][ci] for ci in range(n))
        need_post = any(exp_all[ti][ci] != exp_all[gi][ci] for ci in post_cis)
        if need_full or need_post:
            sep_req.append((gi, need_full, need_post))

    def separable_ok(x):
        for gi, need_full, need_post in sep_req:
            if need_full and all(x[ci] == exp_all[gi][ci] for ci in range(n)):
                return False
            if need_post and all(x[ci] == exp_all[gi][ci] for ci in post_cis):
                return False
        return True

    # ---- 搜索空间：只决策「工单读数 ≠ 原值」出现过的格子 ----
    decision_cis = [ci for ci in range(n) if len(cand_values[ci]) > 1]
    # 强约束格子优先：涉及工单数降序
    decision_cis.sort(key=lambda ci: -len(cell_orders[ci]))
    pos_of = {ci: p for p, ci in enumerate(decision_cis)}

    # k 的下界：每单在「读数≠原值」的格子上至多豁免 1 格
    k_min = 0
    for o in orders:
        d = sum(1 for ci in o["readings"] if o["readings"][ci] != orig[ci])
        k_min = max(k_min, d - 1 if d else 0)
    k_max = len(decision_cis)

    # ---- IDDFS + 分支限界 ----
    x = [None] * n
    mismatch = [0] * len(orders)
    iters = [0]
    solutions = []

    def forced_assignments(upto_pos):
        """对每份已用掉额度的工单，其剩余未决策格子被强制取工单值。
        返回 (forced{ci:value}, conflict)。"""
        forced = {}
        for wi, cells in enumerate(order_cells):
            if mismatch[wi] < 1:
                continue
            for ci in cells:
                if ci not in pos_of or pos_of[ci] < upto_pos:
                    continue  # 已决策
                v = order_value[wi][ci]
                if ci in forced and forced[ci] != v:
                    return forced, True
                forced[ci] = v
        return forced, False

    def dfs(p, changes, k_limit):
        if len(solutions) >= 2 or iters[0] > _MAX_ITERS:
            return
        iters[0] += 1
        forced, conflict = forced_assignments(p)
        if conflict:
            return
        # 下界：已改 + 被强制且非原值的未决策格
        lb = changes + sum(1 for ci, v in forced.items() if v != orig[ci])
        if lb > k_limit:
            return
        if p == len(decision_cis):
            if any(m > 1 for m in mismatch):
                return
            full_x = [orig[ci] if x[ci] is None else x[ci] for ci in range(n)]
            if separable_ok(full_x):
                solutions.append(full_x)
            return
        ci = decision_cis[p]
        if ci in forced:
            values = [forced[ci]]
        else:
            # 原值优先（改动少），再工单值
            values = cand_values[ci]
        for v in values:
            x[ci] = v
            touched = []
            for wi in cell_orders[ci]:
                if v != order_value[wi][ci]:
                    mismatch[wi] += 1
                    touched.append(wi)
            if all(mismatch[wi] <= 1 for wi in touched):
                dfs(p + 1, changes + (1 if v != orig[ci] else 0), k_limit)
            for wi in touched:
                mismatch[wi] -= 1
            x[ci] = None
            if len(solutions) >= 2 or iters[0] > _MAX_ITERS:
                return

    for k in range(k_min, k_max + 1):
        solutions = []
        dfs(0, 0, k)
        if solutions:
            break
    else:
        solutions = []

    base = {
        "fault_id": fault_id,
        "fault_name": faults[ti]["name"],
        "order_count": len(orders),
    }
    if not solutions:
        # k_min == 0 且 k_max == 0 时 dfs 未运行？不会：range 至少含 0。
        # 区分「原表已符合」与「无解」：k=0 可行 ⟺ 每单不符 ≤1
        if k_min == 0 and _orig_column_ok(orders, orig):
            return {**base, "status": "no_change_needed", "min_changes": 0,
                    "changed_cells": [], "table": None}
        return {**base, "status": "no_solution", "min_changes": None,
                "changed_cells": [], "table": None}

    min_k = sum(1 for ci in range(n) if solutions[0][ci] != orig[ci])
    if min_k == 0:
        return {**base, "status": "no_change_needed", "min_changes": 0,
                "changed_cells": [], "table": None}
    if len(solutions) >= 2:
        return {**base, "status": "tied", "min_changes": min_k,
                "changed_cells": [], "table": None}

    best = solutions[0]
    changed = []
    for ci in range(n):
        if best[ci] != orig[ci]:
            changed.append({
                "check_id": checks[ci]["id"],
                "check_name": checks[ci]["name"],
                "phase": checks[ci]["phase"],
                "before": orig_display[ci],
                "after": cand_display[ci][best[ci]],
            })
    new_table = build_calibrated_table(table, fault_id, best, cand_display)
    return {
        **base,
        "status": "unique",
        "min_changes": len(changed),
        "changed_cells": changed,
        "table": new_table,
        "orders_check": _orders_check(orders, best, checks),
    }


def _orig_column_ok(orders, orig):
    """原列是否已满足「每单最多一项不符」。"""
    for o in orders:
        bad = sum(1 for ci, v in o["readings"].items() if v != orig[ci])
        if bad > 1:
            return False
    return True


def _orders_check(orders, x, checks):
    """校订后每份工单的不符项明细（验证「每单最多一项」）。"""
    out = []
    for o in orders:
        bad = [{"check_id": checks[ci]["id"], "order_value": o["display"][ci]}
               for ci, v in o["readings"].items() if v != x[ci]]
        out.append({"order_id": o["id"], "mismatch_count": len(bad),
                    "mismatches": bad})
    return out


def build_calibrated_table(table, fault_id, x, cand_display):
    """生成可重新载入的校订后故障表 JSON（与样例同格式）。"""
    out = {
        "title": table["title"],
        "description": table.get("description", ""),
        "restart_seconds": table["restart_seconds"],
        "faults": [
            {"id": f["id"], "name": f["name"], "hint": f.get("hint", "")}
            for f in table["faults"]
        ],
        "checks": [],
    }
    for ci, c in enumerate(table["checks"]):
        expected = dict(c["expected"])
        expected[fault_id] = cand_display[ci][x[ci]]
        out["checks"].append({
            "id": c["id"],
            "name": c["name"],
            "phase": c["phase"],
            "seconds": c["seconds"],
            "detail": c.get("detail", ""),
            "options": list(c.get("options", [])),
            "expected": expected,
        })
    return out
