"""极小极大诊断规划器：推荐“最坏剩余耗时最短”的下一步。"""

from __future__ import annotations

from functools import lru_cache

from .engine import canonical_reading, PHASE_PRE, PHASE_POST, AnalysisError  # noqa: F401


def _fault_brief(table, fi):
    f = table["faults"][fi]
    return {"id": f["id"], "name": f["name"], "hint": f.get("hint", "")}


def _check_brief(table, ci):
    c = table["checks"][ci]
    return {
        "id": c["id"],
        "name": c["name"],
        "phase": c["phase"],
        "seconds": c["seconds"],
        "detail": c.get("detail", ""),
        "options": list(c.get("options", [])),
    }


def evaluate_readings(table, readings):
    """返回 (recorded{ci: key}, mask, candidates[fi], diagnostics, exp_keys)。"""
    cid_to_ci = {c["id"]: i for i, c in enumerate(table["checks"])}
    nf = len(table["faults"])
    exp = [
        [canonical_reading(c["expected"][f["id"]]) for c in table["checks"]]
        for f in table["faults"]
    ]

    recorded = {}
    mask = 0
    diagnostics = []
    unknown = []
    for cid, raw in (readings or {}).items():
        if raw is None or str(raw).strip() == "":
            continue
        if cid not in cid_to_ci:
            unknown.append(str(cid))
            continue
        ci = cid_to_ci[cid]
        val = canonical_reading(raw)
        if not val:
            continue
        recorded[ci] = val
        mask |= 1 << ci
        single_match = [fi for fi in range(nf) if exp[fi][ci] == val]
        diagnostics.append({
            "check_id": cid,
            "value": str(raw).strip(),
            "canonical_value": val,
            "matches_any": bool(single_match),
            "matching_fault_ids": [table["faults"][fi]["id"] for fi in single_match],
        })

    if unknown:
        raise AnalysisError("读数引用了故障表中不存在的检查项：" + ", ".join(sorted(unknown)))

    candidates = []
    for fi in range(nf):
        if all(exp[fi][ci] == val for ci, val in recorded.items()):
            candidates.append(fi)
    return recorded, mask, tuple(candidates), diagnostics, exp


def indistinguishable_groups(table, candidates, mask, restarted, exp):
    """按“当前仍可做/仍可达的未完成检查”的预期读数签名划分等价类。

    - 未重启：所有未录入检查都仍可达（pre 现在做，post 可重启后做）；
    - 已重启：只剩未录入的 post 检查，未录入的 pre 检查永久错过。
    返回大小 >=2 的组（fault index 元组列表），按组内首个 fault 排序。
    """
    rem = []
    for ci, c in enumerate(table["checks"]):
        if mask >> ci & 1:
            continue
        if restarted and c["phase"] == PHASE_PRE:
            continue
        rem.append(ci)

    sig_groups = {}
    for fi in candidates:
        sig = tuple(exp[fi][ci] for ci in rem)
        sig_groups.setdefault(sig, []).append(fi)

    groups = [tuple(g) for g in sig_groups.values() if len(g) >= 2]
    groups.sort(key=lambda g: g[0])
    return groups, rem


def analyze(table, readings, restarted):
    """分析当前现场状态，给出状态、推荐与歧义组。纯函数。"""
    restarted = bool(restarted)
    n = len(table["checks"])
    secs = [c["seconds"] for c in table["checks"]]
    phases = [c["phase"] for c in table["checks"]]
    restart_s = table["restart_seconds"]
    # 比较键 -> 展示读数
    labels = []
    for c in table["checks"]:
        m = {}
        for display in c["expected"].values():
            if display:
                m.setdefault(canonical_reading(display), display)
        labels.append(m)

    recorded, mask, candidates, diagnostics, exp = evaluate_readings(table, readings)

    result = {
        "status": "ready",
        "restarted": restarted,
        "candidates": [_fault_brief(table, fi) for fi in candidates],
        "candidate_ids": [table["faults"][fi]["id"] for fi in candidates],
        "diagnostics": diagnostics,
        "ambiguous_groups": [],
        "reachable_check_ids": [],
        "recommendation": None,
        "guaranteed": False,
        "worst_remaining_seconds": None,
        "policy_worst_seconds": None,
    }

    groups, rem = indistinguishable_groups(table, candidates, mask, restarted, exp)
    result["reachable_check_ids"] = [table["checks"][ci]["id"] for ci in rem]
    result["ambiguous_groups"] = [
        [_fault_brief(table, fi) for fi in g] for g in groups
    ]

    if len(candidates) == 0:
        result["status"] = "none"
        return result

    if len(candidates) == 1:
        result["status"] = "single"
        fi = candidates[0]
        result["fault"] = _fault_brief(table, fi)
        result["guaranteed"] = True
        result["worst_remaining_seconds"] = 0
        result["policy_worst_seconds"] = 0
        return result

    # 所有检查 + 一次重启的总耗时，作为歧义罚时权重：
    # BIG 大于任何策略可能花费的路径时间，从而优先“最坏歧义组数最小”，
    # 在可以保证唯一查明时退化为精确的最坏剩余耗时最小化。
    total_all = sum(secs) + restart_s
    BIG = total_all + 1

    def partition(cand, ci):
        buckets = {}
        for fi in cand:
            buckets.setdefault(exp[fi][ci], []).append(fi)
        # 稳定顺序：按预期读数在检查 options 中的位置 / 字符串
        return [(val, tuple(idxs)) for val, idxs in sorted(buckets.items(), key=lambda kv: kv[0])]

    @lru_cache(maxsize=None)
    def dp(cand, state_mask, rs):
        cand = tuple(cand)
        if len(cand) == 1:
            return {"u": 0, "kind": "resolved", "key": (0,), "ptime": 0}

        # 候选动作列表，元素为可比较键 + 节点数据
        choices = []

        # 1) 立即停止，接受当前歧义（罚时 BIG*(k-1)）
        stop_u = BIG * (len(cand) - 1)
        choices.append({
            "u": stop_u,
            "sort": (stop_u, 0, 0, "stop"),
            "node": {"kind": "stop", "u": stop_u},
        })

        # 2) 做一项合法且对当前候选有区分度的检查
        for ci in range(n):
            if state_mask >> ci & 1:
                continue
            if phases[ci] == PHASE_PRE and rs:
                continue
            if phases[ci] == PHASE_POST and not rs:
                continue
            branches = partition(cand, ci)
            if len(branches) < 2:
                continue  # 对当前候选无信息量，不可能成为最优下一步
            child_nodes = []
            worst_u = -1
            worst_ptime = 0
            for val, grp in branches:
                child = dp(grp, state_mask | (1 << ci), rs)
                child_nodes.append((val, grp, child))
                worst_u = max(worst_u, child["u"])
                worst_ptime = max(worst_ptime, child.get("ptime", 0))
            u = secs[ci] + worst_u
            ptime = secs[ci] + worst_ptime
            choices.append({
                "u": u,
                "sort": (u, 1, secs[ci], table["checks"][ci]["id"]),
                "node": {
                    "kind": "check", "ci": ci, "u": u, "ptime": ptime,
                    "branches": tuple(child_nodes),
                },
            })

        # 3) 登记并执行重启（最多一次）
        if not rs:
            child = dp(cand, state_mask, True)
            u = restart_s + child["u"]
            ptime = restart_s + child.get("ptime", 0)
            choices.append({
                "u": u,
                "sort": (u, 2, restart_s, "__restart__"),
                "node": {"kind": "restart", "u": u, "ptime": ptime, "child": child},
            })

        best = min(choices, key=lambda ch: ch["sort"])
        node = dict(best["node"])
        node["u"] = best["u"]
        node["ptime"] = best["node"].get("ptime", 0)
        return node

    root = dp(candidates, mask, restarted)

    def serialize(node, cand):
        kind = node["kind"]
        guaranteed = node["u"] < BIG
        out = {
            "kind": kind,
            "guaranteed": guaranteed,
            "score_seconds": node["u"],
            "policy_worst_seconds": node.get("ptime", 0),
            "worst_remaining_seconds": node["u"] if guaranteed else None,
        }
        if kind == "resolved":
            out["kind"] = "resolved"
            out["fault"] = _fault_brief(table, cand[0]) if len(cand) == 1 else None
            out["worst_remaining_seconds"] = 0
            return out
        if kind == "stop":
            return out
        if kind == "check":
            ci = node["ci"]
            out["check"] = _check_brief(table, ci)
            out["seconds"] = secs[ci]
            br = []
            for val, grp, child in node["branches"]:
                br.append({
                    "reading": labels[ci].get(val, val),
                    "reading_key": val,
                    "candidate_ids": [table["faults"][fi]["id"] for fi in grp],
                    "candidate_count": len(grp),
                    "next": serialize(child, grp),
                })
            out["branches"] = br
        elif kind == "restart":
            out["seconds"] = restart_s
            out["restart_seconds"] = restart_s
            out["next_after_restart"] = serialize(node["child"], cand)
        return out

    rec = serialize(root, candidates)
    result["recommendation"] = rec
    result["guaranteed"] = bool(rec["guaranteed"])
    result["worst_remaining_seconds"] = rec["worst_remaining_seconds"]
    result["policy_worst_seconds"] = rec["policy_worst_seconds"]

    # groups 只含 size>=2；因此 “无歧义组” 等价于静态检查可完全区分，
    # 也应与 DP 的 guaranteed 一致（测试中校验该不变量）。
    result["static_separable"] = (len(groups) == 0)
    result["can_guarantee"] = result["guaranteed"]
    return result
