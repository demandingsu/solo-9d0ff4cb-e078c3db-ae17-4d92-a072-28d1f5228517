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
