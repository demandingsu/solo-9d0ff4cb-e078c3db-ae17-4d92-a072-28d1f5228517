"""Flask 排查页应用工厂与 HTTP 路由。

现场状态（readings / restarted / touched）保存在服务端内存中，以签名
cookie 中的 case_id 关联。这样“更正某一条读数”在服务端就是一次局部
更新，结构上保证重启状态与其他读数不被覆盖。

注意：内存案例存储仅适合单进程/单 worker 部署（见 Dockerfile/compose）。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from flask import Flask, jsonify, render_template, request, session

from .engine import (
    TableValidationError,
    spent_seconds,
)
from .planner import AnalysisError, analyze

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TABLE_PATH = os.path.join(ROOT_DIR, "data", "black_screen.json")

# case_id -> 案例状态；单进程演示足够，重启容器即清空。
CASES: dict = {}


def load_table_file(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    # 延迟导入避免循环
    from .engine import normalize_table

    return normalize_table(raw)


def new_case(table: dict) -> dict:
    return {
        "table": table,
        "readings": {},
        "restarted": False,
        # 曾成功录入过非空读数的检查（用于“更正旧读数”与“禁止补做”的判定）
        "touched": [],
        "restart_registered_at": None,
        "created_at": time.time(),
    }


def create_app(table_path: str | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(ROOT_DIR, "templates"),
        static_folder=os.path.join(ROOT_DIR, "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-blackout-diagnostics")

    path = table_path or os.environ.get("FAULT_TABLE_PATH") or DEFAULT_TABLE_PATH
    app.config["FAULT_TABLE_PATH"] = path
    try:
        app.config["DEFAULT_TABLE"] = load_table_file(path)
        app.config["DEFAULT_TABLE_ERROR"] = None
    except (OSError, json.JSONDecodeError, TableValidationError) as exc:  # pragma: no cover
        app.config["DEFAULT_TABLE"] = None
        app.config["DEFAULT_TABLE_ERROR"] = str(exc)

    def get_case():
        cid = session.get("case_id")
        case = CASES.get(cid) if cid else None
        if case is None:
            if app.config["DEFAULT_TABLE"] is None:
                return None
            cid = uuid.uuid4().hex
            case = new_case(app.config["DEFAULT_TABLE"])
            CASES[cid] = case
            session["case_id"] = cid
        return case

    def state_payload(case: dict) -> dict:
        table = case["table"]
        analysis = analyze(table, case["readings"], case["restarted"])
        return {
            "table": table,
            "state": {
                "readings": dict(case["readings"]),
                "restarted": case["restarted"],
                "touched": list(case["touched"]),
                "restart_registered_at": case["restart_registered_at"],
            },
            "analysis": analysis,
            "spent_seconds": spent_seconds(table, case["readings"], case["restarted"]),
            "server_now": time.time(),
        }

    # ---------------- 页面 ----------------
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        ok = app.config["DEFAULT_TABLE"] is not None
        return jsonify({"ok": ok, "service": "blackout-diagnostics"}), (200 if ok else 500)

    @app.get("/api/table/sample")
    def download_sample():
        """下载服务端载入的 JSON 故障表原样文件（默认即黑屏样例）。"""
        sample_path = app.config["FAULT_TABLE_PATH"]
        if not os.path.isfile(sample_path):
            return jsonify({"error": "样例文件不存在"}), 404
        from flask import send_file

        return send_file(sample_path, mimetype="application/json",
                         as_attachment=request.args.get("download") == "1",
                         download_name=os.path.basename(sample_path))

    # ---------------- 有状态排查流程 ----------------
    @app.get("/api/state")
    def api_state():
        case = get_case()
        if case is None:
            return jsonify({"error": "默认故障表加载失败：" + str(app.config["DEFAULT_TABLE_ERROR"])}), 500
        return jsonify(state_payload(case))

    @app.put("/api/readings/<check_id>")
    @app.post("/api/readings/<check_id>")
    def api_put_reading(check_id: str):
        case = get_case()
        if case is None:
            return jsonify({"error": "默认故障表不可用"}), 500

        table = case["table"]
        check = next((c for c in table["checks"] if c["id"] == check_id), None)
        if check is None:
            return jsonify({"error": f"检查项 {check_id} 不存在"}), 404

        if request.files and "value" in request.form:
            raw_value = request.form.get("value")
        elif request.is_json:
            data = request.get_json(silent=True) or {}
            raw_value = data.get("value", "")
        else:
            raw_value = request.form.get("value", request.data.decode("utf-8") if request.data else "")

        if raw_value is None:
            raw_value = ""
        if not isinstance(raw_value, str):
            raw_value = str(raw_value)

        # 阶段约束：post 检查必须先重启；pre 检查重启后只允许更正旧读数
        if check["phase"] == "post" and not case["restarted"]:
            return jsonify({
                "error": f"「{check['name']}」是重启后检查，必须先登记并执行重启。",
                "code": "POST_NEEDS_RESTART",
            }), 409
        if check["phase"] == "pre" and case["restarted"] and check_id not in case["touched"]:
            return jsonify({
                "error": f"「{check['name']}」是重启前检查，重启后不能补做；只能更正在重启前已录入的读数。",
                "code": "PRE_CHECK_MISSED",
            }), 409

        value = raw_value.strip()
        if value == "":
            case["readings"].pop(check_id, None)
        else:
            case["readings"][check_id] = value
            if check_id not in case["touched"]:
                case["touched"].append(check_id)

        return jsonify(state_payload(case))

    @app.delete("/api/readings/<check_id>")
    def api_delete_reading(check_id: str):
        case = get_case()
        if case is None:
            return jsonify({"error": "默认故障表不可用"}), 500
        case["readings"].pop(check_id, None)
        # touched 保留：清空后重填仍属于对旧记录的更正，而不是重启后补做
        return jsonify(state_payload(case))

    @app.post("/api/restart")
    def api_restart():
        case = get_case()
        if case is None:
            return jsonify({"error": "默认故障表不可用"}), 500
        if case["restarted"]:
            return jsonify({
                "error": "本次排查已经登记过重启，最多只能登记一次。",
                "code": "RESTART_LIMIT",
            }), 409
        case["restarted"] = True
        case["restart_registered_at"] = time.time()
        return jsonify(state_payload(case))

    @app.post("/api/reset")
    def api_reset():
        case = get_case()
        if case is None:
            return jsonify({"error": "默认故障表不可用"}), 500
        table = case["table"]
        cid = session.get("case_id")
        CASES[cid] = new_case(table)
        return jsonify(state_payload(CASES[cid]))

    @app.post("/api/table/load")
    def api_table_load():
        """载入维修员自己的 JSON 故障表（JSON body 或 multipart 文件）。"""
        from .engine import normalize_table

        raw = None
        if request.files.get("file") is not None:
            try:
                raw = json.load(request.files["file"].stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return jsonify({"ok": False, "errors": [f"JSON 解析失败：{exc}"]}), 422
        else:
            body = request.get_json(silent=True)
            if body is None:
                return jsonify({"ok": False, "errors": ["请求体不是合法 JSON"]}), 422
            if isinstance(body, dict) and "table" in body and isinstance(body["table"], (dict, list)):
                raw = body["table"]
            else:
                raw = body

        try:
            table = normalize_table(raw)
        except TableValidationError as exc:
            return jsonify({"ok": False, "errors": exc.errors}), 422

        cid = session.get("case_id") or uuid.uuid4().hex
        session["case_id"] = cid
        CASES[cid] = new_case(table)
        payload = state_payload(CASES[cid])
        payload["ok"] = True
        return jsonify(payload)

    # ---------------- 无状态分析（便于外部系统/自动化测试） ----------------
    @app.post("/api/analyze")
    @app.post("/api/recommend")
    def api_analyze():
        from .engine import normalize_table

        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "请求体必须是 JSON 对象"}), 400

        if body.get("table") is not None:
            try:
                table = normalize_table(body["table"])
            except TableValidationError as exc:
                return jsonify({"error": "故障表不合法", "errors": exc.errors}), 422
        else:
            table = app.config["DEFAULT_TABLE"]
            if table is None:
                return jsonify({"error": "默认故障表不可用"}), 500

        readings = body.get("readings") or {}
        if not isinstance(readings, dict):
            return jsonify({"error": "readings 必须是 {检查id: 读数} 对象"}), 400
        restarted = bool(body.get("restarted", False))
        try:
            analysis = analyze(table, readings, restarted)
        except AnalysisError as exc:
            return jsonify({"error": str(exc)}), 400
        analysis["spent_seconds"] = spent_seconds(table, readings, restarted)
        return jsonify({"table": table, "analysis": analysis})

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify({"error": "上传内容过大（上限 2MB）"}), 413

    return app
