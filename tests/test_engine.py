"""引擎 / minimax 规划器单元测试。"""

import json
import os
import random

import pytest

from app.engine import normalize_table, TableValidationError, canonical_reading
from app.planner import analyze

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "black_screen.json")


def load_sample():
    with open(DATA_PATH, encoding="utf-8") as fh:
        return normalize_table(json.load(fh))


def tiny_table():
    return {
        "title": "t",
        "restart_seconds": 100,
        "faults": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
        "checks": [
            {"id": "p", "name": "便宜重启前", "phase": "pre", "seconds": 5,
             "expected": {"A": "x", "B": "y"}},
            {"id": "q", "name": "昂贵重启后", "phase": "post", "seconds": 100,
             "expected": {"A": "u", "B": "v"}},
        ],
    }


# ---------- 归一化 / 校验 ----------

def test_sample_within_limits_and_valid():
    t = load_sample()
    assert 1 <= len(t["faults"]) <= 8
    assert 1 <= len(t["checks"]) <= 10
    assert t["restart_seconds"] >= 0
    fids = {f["id"] for f in t["faults"]}
    for c in t["checks"]:
        assert c["phase"] in ("pre", "post")
        assert set(c["expected"]) == fids
        assert all(v for v in c["expected"].values())


def test_reading_normalization():
    assert canonical_reading("  OSD 正常 ") == "osd 正常"
    assert canonical_reading("12.0") == canonical_reading("12") == "12"
    assert canonical_reading("Ｅ０７")  # NFKC 不报错即可


def test_validation_rejects_too_many_faults_and_missing_expected():
    bad = {
        "restart_seconds": 1,
        "faults": [{"id": f"F{i}"} for i in range(9)],
        "checks": [{"id": "C1", "phase": "pre", "seconds": 1,
                    "expected": {f"F{i}": "x" for i in range(9)}}],
    }
    with pytest.raises(TableValidationError) as ei:
        normalize_table(bad)
    assert any("最多 8" in e for e in ei.value.errors)

    bad2 = {"faults": [{"id": "A"}, {"id": "B"}],
            "checks": [{"id": "C1", "phase": "pre", "expected": {"A": "x"}}]}
    with pytest.raises(TableValidationError):
        normalize_table(bad2)


def test_chinese_phase_aliases_and_fault_row_matrix():
    raw = {
        "故障": [{"编号": "A", "名称": "甲"}, {"编号": "B", "名称": "乙"}],
        "重启耗时": 12,
        "检查": [
            {"编号": "C1", "名称": "灯", "阶段": "重启前", "耗时": 3,
             "预期读数": {"A": "亮", "B": "灭"}},
        ],
    }
    t = normalize_table(raw)
    assert t["restart_seconds"] == 12
    assert t["checks"][0]["phase"] == "pre"
    assert t["checks"][0]["expected"] == {"A": "亮", "B": "灭"}


# ---------- 精确 minimax ----------

def test_cheap_pre_check_beats_expensive_post_route():
    t = normalize_table(tiny_table())
    a = analyze(t, {}, False)
    assert a["status"] == "ready"
    assert a["guaranteed"] is True
    assert a["recommendation"]["kind"] == "check"
    assert a["recommendation"]["check"]["id"] == "p"
    assert a["worst_remaining_seconds"] == 5


def test_restart_recommended_when_it_is_minimax_optimal():
    raw = tiny_table()
    raw["restart_seconds"] = 2
    raw["checks"][0]["seconds"] = 1000   # pre 区分项极贵
    raw["checks"][1]["seconds"] = 1      # post 区分项便宜
    t = normalize_table(raw)
    a = analyze(t, {}, False)
    assert a["guaranteed"] is True
    assert a["recommendation"]["kind"] == "restart"
    assert a["worst_remaining_seconds"] == 3  # 重启 2 + 检查 1


def test_restart_cost_counted_in_worst_time():
    raw = tiny_table()
    raw["restart_seconds"] = 40
    raw["checks"][0]["seconds"] = 30
    raw["checks"][1]["seconds"] = 5
    t = normalize_table(raw)
    # pre p: 30；restart+q: 45 -> 推荐 p，最坏 30
    a = analyze(t, {}, False)
    assert a["recommendation"]["check"]["id"] == "p"
    assert a["worst_remaining_seconds"] == 30
    # 若已决定重启（post 状态），q 只要 5
    a2 = analyze(t, {}, True)
    assert a2["recommendation"]["check"]["id"] == "q"
    assert a2["worst_remaining_seconds"] == 5


def test_premature_restart_creates_ambiguous_pair():
    raw = tiny_table()
    # post 检查对 A/B 读数相同 -> 唯一区分项是 pre 的 p
    raw["checks"][1]["expected"] = {"A": "same", "B": "same"}
    t = normalize_table(raw)

    before = analyze(t, {}, False)
    assert before["guaranteed"] is True
    assert before["recommendation"]["check"]["id"] == "p"

    after = analyze(t, {}, True)
    assert after["status"] == "ready"
    assert after["guaranteed"] is False
    assert [[f["id"] for f in g] for g in after["ambiguous_groups"]] == [["A", "B"]]
    # 没有可区分的 post 检查 -> 推荐停止
    assert after["recommendation"]["kind"] == "stop"
    assert after["policy_worst_seconds"] == 0


def test_no_match_and_single():
    t = load_sample()
    a = analyze(t, {"C2": "不存在的灯色"}, False)
    assert a["status"] == "none"
    assert a["candidates"] == []
    assert any(d["check_id"] == "C2" and d["matches_any"] is False
                for d in a["diagnostics"])

    a2 = analyze(t, {"C4": "OSD无反应"}, False)
    assert a2["status"] == "single"
    assert a2["candidate_ids"] == ["F6"]
    assert a2["worst_remaining_seconds"] == 0


def test_unknown_check_id_raises():
    t = load_sample()
    from app.planner import AnalysisError
    with pytest.raises(AnalysisError):
        analyze(t, {"NOPE": "x"}, False)


# ---------- 样例表的具体教学性质 ----------

def test_sample_immediate_restart_groups():
    t = load_sample()
    a = analyze(t, {}, True)
    groups = [[f["id"] for f in g] for g in a["ambiguous_groups"]]
    assert groups == [["F1", "F2"], ["F3", "F4"], ["F5", "F6"]]
    assert a["guaranteed"] is False


def test_sample_initial_optimum_value():
    t = load_sample()
    a = analyze(t, {}, False)
    assert a["guaranteed"] is True
    assert a["recommendation"]["kind"] == "check"
    assert a["recommendation"]["check"]["id"] == "C2"
    assert a["worst_remaining_seconds"] == 175


# ---------- 按推荐策略模拟每种真实故障 ----------

@pytest.mark.parametrize("fault_index", range(7))
def test_follow_policy_for_each_sample_fault(fault_index):
    t = load_sample()
    fid = t["faults"][fault_index]["id"]
    readings = {}
    restarted = False
    elapsed_path = 0

    for _ in range(20):
        a = analyze(t, readings, restarted)
        if a["status"] == "single":
            assert a["candidate_ids"] == [fid]
            break
        assert a["status"] == "ready"
        r = a["recommendation"]
        if r["kind"] == "check":
            cid = r["check"]["id"]
            c = next(c for c in t["checks"] if c["id"] == cid)
            readings[cid] = c["expected"][fid]
            elapsed_path += c["seconds"]
        elif r["kind"] == "restart":
            restarted = True
            elapsed_path += t["restart_seconds"]
        else:  # stop 意味着无法保证查明 —— 初始可分离的样例表不应走到
            pytest.fail(f"策略在故障 {fid} 下提前 stop：{a['ambiguous_groups']}")
    else:
        pytest.fail("超过最大步数")

    # 沿策略实际路径耗时不应超过根节点给出的最坏剩余耗时
    root = analyze(t, {}, False)
    assert elapsed_path <= root["worst_remaining_seconds"]


# ---------- 随机表不变量：DP guarantee <=> 静态等价类全单元素 ----------

def _random_table(rng, nf, nc):
    faults = [{"id": f"F{i}", "name": f"故障{i}"} for i in range(nf)]
    checks = []
    for j in range(nc):
        k = rng.randint(1, 3)  # 该检查有几种预期读数
        phase = rng.choice(["pre", "post"])
        expected = {f"F{i}": f"v{rng.randrange(k)}" for i in range(nf)}
        checks.append({"id": f"C{j}", "name": f"检查{j}", "phase": phase,
                       "seconds": rng.randint(0, 50), "expected": expected})
    return normalize_table({"restart_seconds": rng.randint(0, 100),
                            "faults": faults, "checks": checks})


def test_guarantee_matches_static_groups_random_tables():
    rng = random.Random(20261126)
    for _case in range(300):
        nf = rng.randint(2, 8)
        nc = rng.randint(1, 10)
        t = _random_table(rng, nf, nc)

        # 状态 1：空读数，未重启 / 已重启
        for restarted in (False, True):
            a = analyze(t, {}, restarted)
            if len(a["candidates"]) <= 1:
                continue
            has_group = len(a["ambiguous_groups"]) > 0
            assert a["guaranteed"] == (not has_group)

        # 状态 2：以某个随机故障为真值，录入其 0~全部 条读数
        true_fi = rng.randrange(nf)
        fid = t["faults"][true_fi]["id"]
        readings = {}
        for c in t["checks"]:
            if rng.random() < 0.5:
                readings[c["id"]] = c["expected"][fid]
        restarted = rng.choice([False, True])
        a = analyze(t, readings, restarted)
        assert fid in a["candidate_ids"] or a["status"] == "single"
        if a["status"] == "ready":
            assert a["guaranteed"] == (len(a["ambiguous_groups"]) == 0)
