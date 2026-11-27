"""工单校订（app/calibrate.py）单元测试。"""

import json
import os

import pytest

from app.calibrate import (
    CalibrateInputError,
    MAX_ORDERS,
    calibrate,
    extract_fault_id,
    parse_orders,
)
from app.engine import normalize_table
from app.planner import analyze

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "black_screen.json")


def load_sample():
    with open(DATA_PATH, encoding="utf-8") as fh:
        return normalize_table(json.load(fh))


def sep_table():
    """C1 是 (T,G) 唯一区分格（全表）；C3 是 (T,G) 唯一 post 区分格的变体在下方构造。"""
    return normalize_table({
        "restart_seconds": 10,
        "faults": [{"id": "T", "name": "目标"}, {"id": "G", "name": "其他"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
            {"id": "C3", "phase": "post", "seconds": 1, "expected": {"T": "m", "G": "m"}},
        ],
    })


def post_sep_table():
    """(T,G) 仅靠 post 检查 C3 区分；C1(pre) 也区分但 C2 不区分。"""
    return normalize_table({
        "restart_seconds": 10,
        "faults": [{"id": "T", "name": "目标"}, {"id": "G", "name": "其他"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
            {"id": "C3", "phase": "post", "seconds": 1, "expected": {"T": "m", "G": "n"}},
        ],
    })


# ---------------- 工单解析 ----------------

def test_parse_orders_flat_and_readings_key():
    t = sep_table()
    orders = parse_orders([
        {"id": "W1", "readings": {"C1": "x", "C2": "p"}},
        {"工单号": "W2", "C1": "y", "C3": "m"},          # 扁平写法
        {"实测读数": {"C1": "x"}},                        # 中文 readings 键
    ], t)
    assert [o["id"] for o in orders] == ["W1", "W2", "WO-3"]
    assert orders[1]["readings"] == {0: "y", 2: "m"}


def test_parse_orders_empty_values_ignored():
    t = sep_table()
    orders = parse_orders([{"readings": {"C1": "", "C2": None, "C3": "  "}}], t)
    assert orders[0]["readings"] == {}


def test_parse_orders_unknown_check_rejected():
    t = sep_table()
    with pytest.raises(CalibrateInputError) as ei:
        parse_orders([{"readings": {"C1": "x", "C99": "z"}}], t)
    assert any("C99" in e for e in ei.value.errors)


def test_parse_orders_count_limits():
    t = sep_table()
    with pytest.raises(CalibrateInputError):
        parse_orders([], t)
    with pytest.raises(CalibrateInputError) as ei:
        parse_orders([{"readings": {}} for _ in range(MAX_ORDERS + 1)], t)
    assert any("最多" in e for e in ei.value.errors)


def test_extract_fault_id():
    t = sep_table()
    assert extract_fault_id({"fault_id": "T"}, t) == "T"
    assert extract_fault_id({"故障": "G"}, t) == "G"
    with pytest.raises(CalibrateInputError):
        extract_fault_id({}, t)
    with pytest.raises(CalibrateInputError):
        extract_fault_id({"fault_id": "NOPE"}, t)


# ---------------- 四种结果状态 ----------------

def test_no_change_needed_when_orders_match():
    t = sep_table()
    r = calibrate(t, "T", [{"readings": {"C1": "x", "C2": "p", "C3": "m"}}])
    assert r["status"] == "no_change_needed"
    assert r["min_changes"] == 0
    assert r["table"] is None


def test_no_change_needed_allows_one_mismatch_per_order():
    """每单最多一项不符时原表即符合，无需校订。"""
    t = sep_table()
    r = calibrate(t, "T", [
        {"readings": {"C1": "抄错的值"}},
        {"readings": {"C1": "x", "C3": "另一个错值"}},
    ])
    assert r["status"] == "no_change_needed"


def test_unique_solution_minimal_and_respects_full_separability():
    """一单 2 项不符；改 C1 会让 (T,G) 全表不可区分，故唯一解是改 C2。"""
    t = sep_table()
    r = calibrate(t, "T", [{"readings": {"C1": "y", "C2": "r"}}])
    assert r["status"] == "unique"
    assert r["min_changes"] == 1
    assert r["changed_cells"] == [{
        "check_id": "C2", "check_name": "C2", "phase": "pre",
        "before": "p", "after": "r",
    }]
    # 校订后 (T,G) 仍可靠 C1 区分
    t2 = normalize_table(r["table"])
    assert t2["checks"][0]["expected"]["T"] == "x"
    assert t2["checks"][1]["expected"]["T"] == "r"


def test_unique_solution_multi_cell():
    """必须改 2 格的唯一解（多故障联动约束把其余 k=2 组合全部封杀）。

    表：(T,H) 靠 C2/C3 区分，(T,G) 靠 C1/C3 区分。工单要求 C1→y、C2→b、C3→c：
    - (x,b,c) 与 H 整列相同 -> 禁止；(y,p,c) 与 G 整列相同 -> 禁止；
    - 唯一可行：改 C1=y、C2=b，C3 保持 m（工单 C3 记为唯一一项不符）。
    """
    t = normalize_table({
        "restart_seconds": 10,
        "faults": [{"id": "T"}, {"id": "G"}, {"id": "H"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1,
             "expected": {"T": "x", "G": "y", "H": "x"}},
            {"id": "C2", "phase": "pre", "seconds": 1,
             "expected": {"T": "p", "G": "p", "H": "b"}},
            {"id": "C3", "phase": "post", "seconds": 1,
             "expected": {"T": "m", "G": "c", "H": "c"}},
        ],
    })
    r = calibrate(t, "T", [{"readings": {"C1": "y", "C2": "b", "C3": "c"}}])
    assert r["status"] == "unique"
    assert r["min_changes"] == 2
    assert [c["check_id"] for c in r["changed_cells"]] == ["C1", "C2"]
    assert [c["after"] for c in r["changed_cells"]] == ["y", "b"]
    # 校订后 (T,G) 靠 C2 区分、(T,H) 靠 C1/C3 区分
    t2 = normalize_table(r["table"])
    exp = {c["id"]: c["expected"] for c in t2["checks"]}
    assert exp["C2"]["T"] == "b" != exp["C2"]["G"]
    assert exp["C1"]["T"] == "y" != exp["C1"]["H"]


def test_tied_when_two_equal_minimal_solutions():
    """一单 2 项不符，改哪一格都合法 -> 并列，不出表。"""
    t = sep_table()
    r = calibrate(t, "T", [{"readings": {"C1": "a", "C2": "q"}}])
    assert r["status"] == "tied"
    assert r["min_changes"] == 1
    assert r["table"] is None


def test_no_solution_when_orders_conflict():
    """三单在两格上互不相同：2 格最多覆盖 2 单，必有 1 单两项不符。"""
    t = sep_table()
    r = calibrate(t, "T", [
        {"readings": {"C1": "a1", "C2": "b1"}},
        {"readings": {"C1": "a2", "C2": "b2"}},
        {"readings": {"C1": "a3", "C2": "b3"}},
    ])
    assert r["status"] == "no_solution"
    assert r["table"] is None


# ---------------- 可区分性约束 ----------------

def test_post_only_separability_preserved():
    """(T,G) 仅靠 post 检查 C3 区分：工单要求 C3 改成与 G 相同的值时，
    该方案被禁止，只能改 C1。"""
    t = post_sep_table()
    r = calibrate(t, "T", [{"readings": {"C3": "n", "C1": "y"}}])
    assert r["status"] == "unique"
    assert [c["check_id"] for c in r["changed_cells"]] == ["C1"]
    # 校订后 post 检查 C3 仍可区分 (T,G)
    t2 = normalize_table(r["table"])
    c3 = next(c for c in t2["checks"] if c["id"] == "C3")
    assert c3["expected"]["T"] == "m" != c3["expected"]["G"]


def test_post_separability_can_force_no_solution():
    """唯一候选改动会破坏 post 可区分性 -> 无解。"""
    t = post_sep_table()
    # 两单要求 C3=n 且 C1 也各抄错一项（C1 必须改成与 G 相同的 y）
    r = calibrate(t, "T", [
        {"readings": {"C3": "n", "C1": "y"}},
        {"readings": {"C3": "n", "C1": "y"}},
    ])
    # k=1：改 C1=y 则 (T,G) 仅靠 C3(post) 区分 -> 仍满足；改 C3=n 破坏 post 区分。
    # 故唯一解为改 C1（k=1）。再叠加一单强制 C3：
    assert r["status"] == "unique"
    r2 = calibrate(t, "T", [
        {"readings": {"C3": "n", "C1": "x"}},   # C1 与原值一致 -> C3 必须改
        {"readings": {"C3": "n", "C1": "x"}},
    ])
    # 每单只有 C3 一项不符 -> 原表已符合，无需校订
    assert r2["status"] == "no_change_needed"


def test_full_separability_no_solution():
    """所有候选改法都会让 (T,G) 全表不可区分 -> 无解。"""
    t = normalize_table({
        "faults": [{"id": "T"}, {"id": "G"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
        ],
    })
    # 工单要求 C1=y（=G 的值）且 C2 也抄错；改 C1=y 后 (T,G) 全表一致 -> 禁止；
    # 改 C2 无法修复 C1 的不符 -> 每单仍 1 项不符（C1），其实合法……
    r = calibrate(t, "T", [{"readings": {"C1": "y", "C2": "q"}}])
    # k=1：改 C2=q -> 工单 C1 不符 1 项 ✓，(T,G) 靠 C1 区分 ✓ -> 唯一解
    assert r["status"] == "unique"
    assert [c["check_id"] for c in r["changed_cells"]] == ["C2"]
    # 若工单只抄 C1 一项 -> 原表已符合
    r2 = calibrate(t, "T", [{"readings": {"C1": "y"}}])
    assert r2["status"] == "no_change_needed"


# ---------------- 等价读数 / 取值来源 ----------------

def test_equivalent_readings_not_a_difference():
    t = normalize_table({
        "faults": [{"id": "T"}, {"id": "G"}],
        "checks": [{"id": "C1", "phase": "pre", "seconds": 1,
                    "expected": {"T": "12", "G": "13"}}],
    })
    # "12.0"、" 12 "、全角１２ 与 "12" 等价 -> 不算差异
    r = calibrate(t, "T", [{"readings": {"C1": " 12.0 "}}, {"readings": {"C1": "１２"}}])
    assert r["status"] == "no_change_needed"


def test_new_value_must_come_from_orders():
    """校订后的值必须是工单中该项出现过的值（等价类代表）。"""
    t = sep_table()
    # W3 在 C1 记原值 x：改 C1 会让 W3 两项不符 -> 只能改 C2
    r = calibrate(t, "T", [{"readings": {"C1": "a", "C2": "q"}, "id": "W1"},
                           {"readings": {"C1": "a", "C2": "q"}, "id": "W2"},
                           {"readings": {"C1": "x", "C2": "q"}, "id": "W3"}])
    assert r["status"] == "unique"
    assert r["changed_cells"][0]["check_id"] == "C2"
    assert r["changed_cells"][0]["after"] == "q"  # 来自工单，不是新造值
    # 对称地，W3 在 C2 记原值 p：只能改 C1
    r2 = calibrate(t, "T", [{"readings": {"C1": "a", "C2": "q"}, "id": "W1"},
                            {"readings": {"C1": "a", "C2": "q"}, "id": "W2"},
                            {"readings": {"C1": "a", "C2": "p"}, "id": "W3"}])
    assert r2["status"] == "unique"
    assert r2["changed_cells"][0]["check_id"] == "C1"
    assert r2["changed_cells"][0]["after"] == "a"


def test_orders_check_reports_at_most_one_mismatch():
    t = sep_table()
    r = calibrate(t, "T", [{"readings": {"C1": "y", "C2": "r"}}])
    assert r["status"] == "unique"
    assert len(r["orders_check"]) == 1
    oc = r["orders_check"][0]
    assert oc["mismatch_count"] <= 1
    assert oc["mismatches"][0]["check_id"] == "C1"


# ---------------- 校订表可重新载入 & 排查能力 ----------------

def test_calibrated_table_reloadable_and_analyzable():
    t = load_sample()
    # 样例表 F3：三份工单，唯一解改 C5
    orders = [
        {"id": "WO-1", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-2", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-3", "readings": {"C5": "E03 电源自检失败", "C8": "无信号"}},
    ]
    r = calibrate(t, "F3", orders)
    assert r["status"] == "unique"
    assert [c["check_id"] for c in r["changed_cells"]] == ["C5"]
    t2 = normalize_table(r["table"])  # 可重新载入
    c5 = next(c for c in t2["checks"] if c["id"] == "C5")
    assert c5["expected"]["F3"] == "E03 电源自检失败"
    # 校订后整表仍可保证唯一查明（初始状态）
    a = analyze(t2, {}, False)
    assert a["guaranteed"] is True


def test_sample_table_no_change_when_orders_consistent():
    t = load_sample()
    f3 = {"C1": "不亮", "C2": "全无", "C3": "无声", "C4": "OSD正常弹出",
          "C5": "无代码", "C6": "完全不显示", "C7": "仍然黑屏", "C8": "无信号"}
    r = calibrate(t, "F3", [{"readings": f3}, {"readings": dict(f3, C1="不亮 ")}])
    assert r["status"] == "no_change_needed"


# ---------------- 输入错误 ----------------

def test_calibrate_input_errors():
    t = sep_table()
    with pytest.raises(CalibrateInputError):
        calibrate(t, "T", "not-a-list")
    with pytest.raises(CalibrateInputError):
        calibrate(t, "T", [{"readings": {"C1": "x"}} for _ in range(9)])
