#!/usr/bin/env python3
"""对运行中的服务做端到端 HTTP 冒烟测试（标准库实现，无需 requests）。

用法：
    python3 scripts/smoke.py [BASE_URL]
默认 BASE_URL=http://127.0.0.1:8000
"""

import json
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw}


def wait_health():
    for _ in range(40):
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    print(f"== smoke against {BASE} ==")
    if not wait_health():
        print("服务未就绪，放弃")
        sys.exit(2)

    # ---- 会话 A：完整最优流程，最终定位 F5 ----
    a = Client()
    st, body = a.call("GET", "/api/state")
    check("GET /api/state 200", st == 200)
    an = body["analysis"]
    check("初始保证可查明", an["guaranteed"] is True)
    check("初始推荐 C2", an["recommendation"]["check"]["id"] == "C2", str(an["recommendation"]))
    check("初始最坏剩余 175s", an["worst_remaining_seconds"] == 175, str(an["worst_remaining_seconds"]))

    for cid, val in [("C2", "蓝常亮"), ("C4", "OSD正常弹出"), ("C5", "无代码")]:
        st, body = a.call("PUT", f"/api/readings/{cid}", {"value": val})
        check(f"录入 {cid} -> 200", st == 200, str(body))

    check("三条读数后候选 F4/F5", body["analysis"]["candidate_ids"] == ["F4", "F5"],
          str(body["analysis"]["candidate_ids"]))
    check("此时推荐重启", body["analysis"]["recommendation"]["kind"] == "restart")

    st, body = a.call("POST", "/api/restart")
    check("登记重启 200", st == 200)
    check("重启耗时计入已用时间", body["spent_seconds"] == 10 + 25 + 30 + 90, str(body["spent_seconds"]))

    st2, _ = a.call("POST", "/api/restart")
    check("第二次重启 409", st2 == 409)

    st, body = a.call("PUT", "/api/readings/C1", {"value": "绿常亮"})
    check("重启后补做未录过的 C1 被拒(409)", st == 409 and body.get("code") == "PRE_CHECK_MISSED")

    # 更正已录入的 C5（提交相同值），重启状态与其他读数必须保留
    st, body = a.call("PUT", "/api/readings/C5", {"value": "无代码"})
    check("更正旧读数 200", st == 200)
    check("更正后重启状态保留", body["state"]["restarted"] is True)
    check("更正后其他读数保留",
          body["state"]["readings"].get("C2") == "蓝常亮"
          and body["state"]["readings"].get("C4") == "OSD正常弹出")

    st, body = a.call("PUT", "/api/readings/C8", {"value": "识别成功"})
    check("录入 C8 后唯一查明 F5",
          body["analysis"]["status"] == "single"
          and body["analysis"]["candidate_ids"] == ["F5"],
          str(body["analysis"].get("candidate_ids")))

    st, body = a.call("POST", "/api/reset")
    check("重置后无读数且未重启",
          st == 200 and body["state"]["readings"] == {} and body["state"]["restarted"] is False)

    # ---- 会话 B：立刻重启 -> 三个歧义组 ----
    b = Client()
    st, body = b.call("POST", "/api/restart")
    groups = [[f["id"] for f in g] for g in body["analysis"]["ambiguous_groups"]]
    check("立刻重启返回 3 个歧义组",
          groups == [["F1", "F2"], ["F3", "F4"], ["F5", "F6"]], str(groups))
    check("立刻重启不保证查明", body["analysis"]["guaranteed"] is False)

    # ---- 会话 C：读数无匹配 ----
    c = Client()
    st, body = c.call("PUT", "/api/readings/C2", {"value": "彩虹色跑马灯"})
    check("离奇读数返回 none", st == 200 and body["analysis"]["status"] == "none")

    # ---- 无状态分析：合成小表精确断言 ----
    tiny = {
        "restart_seconds": 2,
        "faults": [{"id": "A"}, {"id": "B"}],
        "checks": [
            {"id": "p", "phase": "pre", "seconds": 1000, "expected": {"A": "x", "B": "y"}},
            {"id": "q", "phase": "post", "seconds": 1, "expected": {"A": "u", "B": "v"}},
        ],
    }
    st, body = c.call("POST", "/api/analyze", {"table": tiny})
    aa = body["analysis"]
    check("无状态分析推荐重启且最坏 3s",
          st == 200 and aa["recommendation"]["kind"] == "restart"
          and aa["worst_remaining_seconds"] == 3, str(aa))

    # ---- 会话 D：工单校订（样例表 F3，唯一解改 C5）----
    d = Client()
    orders = [
        {"id": "WO-1", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-2", "readings": {"C5": "E03 电源自检失败", "C8": "无信号输出"}},
        {"id": "WO-3", "readings": {"C5": "E03 电源自检失败", "C8": "无信号"}},
    ]
    st, body = d.call("POST", "/api/calibrate", {"fault_id": "F3", "orders": orders})
    check("校订返回 200 且方案唯一", st == 200 and body.get("status") == "unique", str(body)[:200])
    cells = body.get("changed_cells", [])
    check("唯一解改 C5（无代码 -> E03 电源自检失败）",
          len(cells) == 1 and cells[0]["check_id"] == "C5"
          and cells[0]["before"] == "无代码" and cells[0]["after"] == "E03 电源自检失败",
          str(cells))
    check("每单最多一项不符",
          all(o["mismatch_count"] <= 1 for o in body.get("orders_check", [])))
    st, body2 = d.call("POST", "/api/table/load", body.get("table"))
    check("校订表可重新载入且仍可保证查明",
          st == 200 and body2["analysis"]["guaranteed"] is True)
    c5 = next(c for c in body2["table"]["checks"] if c["id"] == "C5")
    check("载入后 C5/F3 预期值已更新", c5["expected"]["F3"] == "E03 电源自检失败")

    # 原表已符合 -> 无需校订
    st, body = d.call("POST", "/api/calibrate", {
        "fault_id": "F3",
        "orders": [{"readings": {"C1": "不亮", "C2": "全无"}}],
    })
    check("一致工单 -> 无需校订", st == 200 and body.get("status") == "no_change_needed")

    # 并列 / 无解 / 输入错误
    tiny2 = {
        "faults": [{"id": "T"}, {"id": "G"}],
        "checks": [
            {"id": "C1", "phase": "pre", "seconds": 1, "expected": {"T": "x", "G": "y"}},
            {"id": "C2", "phase": "pre", "seconds": 1, "expected": {"T": "p", "G": "p"}},
        ],
    }
    st, body = d.call("POST", "/api/calibrate", {
        "table": tiny2, "fault_id": "T",
        "orders": [{"readings": {"C1": "a", "C2": "q"}}]})
    check("两种改法 -> 并列不出表", st == 200 and body.get("status") == "tied"
          and body.get("table") is None)
    st, body = d.call("POST", "/api/calibrate", {
        "table": tiny2, "fault_id": "T",
        "orders": [{"readings": {"C1": "a1", "C2": "b1"}},
                   {"readings": {"C1": "a2", "C2": "b2"}},
                   {"readings": {"C1": "a3", "C2": "b3"}}]})
    check("三单冲突 -> 无解", st == 200 and body.get("status") == "no_solution")
    st, body = d.call("POST", "/api/calibrate", {
        "fault_id": "F3", "orders": [{"readings": {}} for _ in range(9)]})
    check("工单超过 8 份 -> 422", st == 422)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
