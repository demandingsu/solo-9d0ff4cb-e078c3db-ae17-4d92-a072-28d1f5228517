"""HTTP 路由 + 服务端案例状态/阶段约束测试。"""

import pytest

from app import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def tiny_raw():
    return {
        "title": "接口测试表",
        "restart_seconds": 8,
        "faults": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
        "checks": [
            {"id": "p", "name": "重启前检查", "phase": "pre", "seconds": 4,
             "expected": {"A": "x", "B": "y"}},
            {"id": "q", "name": "重启后检查", "phase": "post", "seconds": 6,
             "expected": {"A": "u", "B": "v"}},
        ],
    }


def test_index_and_health(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "黑屏" in r.get_data(as_text=True)
    h = client.get("/healthz")
    assert h.status_code == 200
    assert h.get_json()["ok"] is True


def test_state_loads_sample(client):
    j = client.get("/api/state").get_json()
    assert len(j["table"]["faults"]) == 7
    assert len(j["table"]["checks"]) == 8
    assert j["analysis"]["status"] == "ready"
    assert j["analysis"]["guaranteed"] is True
    assert j["state"]["restarted"] is False


def test_reading_update_preserves_other_readings_and_restart(client):
    client.put("/api/readings/C2", json={"value": "蓝常亮"})
    client.put("/api/readings/C4", json={"value": "OSD正常弹出"})
    r = client.post("/api/restart")
    assert r.status_code == 200
    body = r.get_json()
    assert body["state"]["restarted"] is True
    assert body["state"]["readings"] == {"C2": "蓝常亮", "C4": "OSD正常弹出"}

    # 更正一条旧读数：重启状态与另一条读数必须保留
    r2 = client.put("/api/readings/C4", json={"value": "OSD无反应"})
    assert r2.status_code == 200
    b2 = r2.get_json()
    assert b2["state"]["restarted"] is True
    assert b2["state"]["readings"]["C2"] == "蓝常亮"
    assert b2["state"]["readings"]["C4"] == "OSD无反应"
    assert b2["analysis"]["status"] == "single"
    assert b2["analysis"]["candidate_ids"] == ["F6"]


def test_restart_only_once(client):
    assert client.post("/api/restart").status_code == 200
    r = client.post("/api/restart")
    assert r.status_code == 409
    assert r.get_json()["code"] == "RESTART_LIMIT"


def test_post_check_blocked_before_restart(client):
    r = client.put("/api/readings/C6", json={"value": "Logo正常"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "POST_NEEDS_RESTART"


def test_pre_check_cannot_be_made_up_after_restart_but_correction_ok(client):
    # 没在重启前录入 C1 -> 重启后补做被拒
    client.post("/api/restart")
    r = client.put("/api/readings/C1", json={"value": "绿常亮"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "PRE_CHECK_MISSED"

    # 新会话：重启前录过 C1 -> 重启后允许更正
    client.post("/api/reset")
    assert client.put("/api/readings/C1", json={"value": "绿常亮"}).status_code == 200
    client.post("/api/restart")
    r2 = client.put("/api/readings/C1", json={"value": "不亮"})
    assert r2.status_code == 200
    assert r2.get_json()["state"]["readings"]["C1"] == "不亮"


def test_clearing_reading_keeps_others(client):
    client.put("/api/readings/C2", json={"value": "蓝常亮"})
    client.put("/api/readings/C3", json={"value": "运转正常"})
    r = client.delete("/api/readings/C2")
    b = r.get_json()
    assert "C2" not in b["state"]["readings"]
    assert b["state"]["readings"] == {"C3": "运转正常"}


def test_reset_clears_case(client):
    client.put("/api/readings/C2", json={"value": "蓝常亮"})
    client.post("/api/restart")
    b = client.post("/api/reset").get_json()
    assert b["state"]["readings"] == {}
    assert b["state"]["restarted"] is False


def test_stateless_analyze_exact_recommendation(client):
    r = client.post("/api/analyze", json={"table": tiny_raw(), "readings": {}, "restarted": False})
    assert r.status_code == 200
    a = r.get_json()["analysis"]
    assert a["guaranteed"] is True
    assert a["recommendation"]["check"]["id"] == "p"
    assert a["worst_remaining_seconds"] == 4

    alias = client.post("/api/recommend", json={"table": tiny_raw()})
    assert alias.status_code == 200
    assert alias.get_json()["analysis"]["recommendation"]["check"]["id"] == "p"


def test_stateless_analyze_unknown_check_400(client):
    r = client.post("/api/analyze", json={"readings": {"X": "1"}})
    assert r.status_code == 400


def test_table_upload_validation_and_use(client):
    bad = client.post("/api/table/load", json={"faults": [], "checks": []})
    assert bad.status_code == 422
    assert bad.get_json()["ok"] is False
    assert bad.get_json()["errors"]

    ok = client.post("/api/table/load", json=tiny_raw())
    assert ok.status_code == 200
    body = ok.get_json()
    assert body["table"]["title"] == "接口测试表"
    assert body["analysis"]["recommendation"]["check"]["id"] == "p"

    # 上传后的表驱动同一会话流程
    r = client.put("/api/readings/p", json={"value": "x"})
    assert r.status_code == 200
    assert r.get_json()["analysis"]["candidate_ids"] == ["A"]


def test_sample_download_route(client):
    r = client.get("/api/table/sample")
    assert r.status_code == 200
    assert r.get_json()["restart_seconds"] == 90


def test_multipart_file_upload(client):
    import io
    import json as _json

    data = {"file": (io.BytesIO(_json.dumps(tiny_raw()).encode("utf-8")), "t.json")}
    r = client.post("/api/table/load", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["table"]["title"] == "接口测试表"


# ---------------- 工单校订 API ----------------

def cal_orders_sample():
    """样例表 F3 的三份工单：唯一解改 C5。"""
    return [
        {"id": "WO-1", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-2", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-3", "readings": {"C5": "E03 电源自检失败", "C8": "无信号"}},
    ]


def test_calibrate_unique_against_session_table(client):
    r = client.post("/api/calibrate",
                    json={"fault_id": "F3", "orders": cal_orders_sample()})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status"] == "unique"
    assert body["min_changes"] == 1
    assert body["changed_cells"] == [{
        "check_id": "C5", "check_name": "重启前面板故障代码", "phase": "pre",
        "before": "无代码", "after": "E03 电源自检失败",
    }]
    # 每单最多一项不符
    assert all(o["mismatch_count"] <= 1 for o in body["orders_check"])
    # 校订表可重新载入并驱动会话
    r2 = client.post("/api/table/load", json=body["table"])
    assert r2.status_code == 200
    t = r2.get_json()["table"]
    c5 = next(c for c in t["checks"] if c["id"] == "C5")
    assert c5["expected"]["F3"] == "E03 电源自检失败"
    assert r2.get_json()["analysis"]["guaranteed"] is True


def test_calibrate_no_change_needed(client):
    r = client.post("/api/calibrate", json={
        "fault": "F3",
        "orders": [{"readings": {"C1": "不亮", "C2": "全无", "C5": "无代码"}}],
    })
    assert r.status_code == 200
    assert r.get_json()["status"] == "no_change_needed"
    assert r.get_json()["table"] is None


def test_calibrate_tied_and_no_solution(client):
    tiny = {
        "faults": [{"id": "T"}, {"id": "G"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
        ],
    }
    r = client.post("/api/calibrate", json={
        "table": tiny, "fault_id": "T",
        "orders": [{"readings": {"C1": "a", "C2": "q"}}],
    })
    assert r.get_json()["status"] == "tied"
    assert r.get_json()["table"] is None

    r2 = client.post("/api/calibrate", json={
        "table": tiny, "fault_id": "T",
        "orders": [
            {"readings": {"C1": "a1", "C2": "b1"}},
            {"readings": {"C1": "a2", "C2": "b2"}},
            {"readings": {"C1": "a3", "C2": "b3"}},
        ],
    })
    assert r2.get_json()["status"] == "no_solution"


def test_calibrate_input_errors(client):
    # 缺故障编号
    r = client.post("/api/calibrate", json={"orders": [{"readings": {}}]})
    assert r.status_code == 422
    assert r.get_json()["ok"] is False
    # 故障不存在
    r = client.post("/api/calibrate", json={"fault_id": "FX", "orders": [{"readings": {}}]})
    assert r.status_code == 422
    # 工单数超限
    r = client.post("/api/calibrate", json={
        "fault_id": "F3", "orders": [{"readings": {}} for _ in range(9)]})
    assert r.status_code == 422
    assert any("最多" in e for e in r.get_json()["errors"])
    # 未知检查编号
    r = client.post("/api/calibrate", json={
        "fault_id": "F3", "orders": [{"readings": {"C99": "x"}}]})
    assert r.status_code == 422
    assert any("C99" in e for e in r.get_json()["errors"])
    # 非 JSON 请求体
    r = client.post("/api/calibrate", data="not json",
                    content_type="application/json")
    assert r.status_code == 400


def test_calibrate_uses_loaded_custom_table(client):
    """载入自定义表后，校订作用于该表（会话表）。"""
    tiny = {
        "faults": [{"id": "T"}, {"id": "G"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
        ],
    }
    assert client.post("/api/table/load", json=tiny).status_code == 200
    r = client.post("/api/calibrate", json={
        "fault_id": "T",
        "orders": [{"readings": {"C1": "y", "C2": "q"}},
                   {"readings": {"C1": "y", "C2": "p"}}],
    })
    body = r.get_json()
    # k=1 改 C1=y 会破坏 (T,G) 区分；改 C2=q 会让工单2（C2 记原值 p）两项不符。
    # 唯一解是 k=2：C1→y、C2→q，(T,G) 转而靠 C2 区分。
    assert body["status"] == "unique"
    assert body["min_changes"] == 2
    assert [c["check_id"] for c in body["changed_cells"]] == ["C1", "C2"]
