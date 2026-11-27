"""黑屏故障排查引擎。

不依赖 Flask，可单独用于测试 / 命令行 / 其他前端。

核心概念
--------
- 故障表（table）：最多 8 个故障、最多 10 项检查，每项检查给出
  每个故障对应的“预期读数”、耗时、阶段（pre=重启前 / post=重启后）。
- 现场状态：readings（check_id -> 实测读数）、restarted（是否已重启）。
- 推荐算法：在“已记录读数 + 已重启状态”确定的状态空间上做极小极大 DP，
  选择使“查明故障的最坏剩余耗时”最短的下一步（检查或重启）。
- 重启不可逆且最多一次：pre 检查只在未重启时可做；重启后未录入的 pre
  检查永久错过；post 检查只有重启后可做。已录入读数始终保留并继续缩小范围。
- 当唯一查明不可能时，用“剩余仍可做的检查”上的预期读数等价类给出
  仍分不清的故障组；DP 使用足够大的歧义罚时，因此在无法保证查明时
  仍会给出优先缩小歧义组的“尽力推荐”。
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

MAX_FAULTS = 8
MAX_CHECKS = 10

PHASE_PRE = "pre"
PHASE_POST = "post"

_PHASE_ALIASES = {
    "pre": PHASE_PRE,
    "before": PHASE_PRE,
    "before_restart": PHASE_PRE,
    "restart_before": PHASE_PRE,
    "pre_restart": PHASE_PRE,
    "重启前": PHASE_PRE,
    "重启前检查": PHASE_PRE,
    "重启之前": PHASE_PRE,
    "post": PHASE_POST,
    "after": PHASE_POST,
    "after_restart": PHASE_POST,
    "restart_after": PHASE_POST,
    "post_restart": PHASE_POST,
    "重启后": PHASE_POST,
    "重启后检查": PHASE_POST,
    "重启之后": PHASE_POST,
}

_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class TableValidationError(ValueError):
    """故障表不合法；errors 为逐条中文错误说明。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class AnalysisError(ValueError):
    """分析请求状态不合法（如读数引用了不存在的检查项）。"""


def _normalize(value, fold_case: bool) -> str:
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value)).strip()
    if fold_case:
        s = s.casefold()
    s = re.sub(r"\s+", " ", s)
    if s and _NUM_RE.match(s):
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
            return ("%g" % f)
        except ValueError:
            pass
    return s


def clean_reading(value) -> str:
    """展示用归一化：NFKC、去空白、折叠空格，但保留英文大小写。"""
    return _normalize(value, fold_case=False)


def canonical_reading(value) -> str:
    """比较用键：在 clean_reading 基础上英文大小写不敏感。

    - NFKC（全角转半角等）、去首尾空白、折叠连续空白；
    - 纯数字按数值归一化（"12.0" 与 "12" 视为相同）。
    """
    return _normalize(value, fold_case=True)


def _as_nonneg_number(value, field, errors, ctx):
    if isinstance(value, bool):
        errors.append(f"{ctx}{field} 必须是非负数字")
        return 0
    if isinstance(value, (int, float)):
        n = value
    elif isinstance(value, str) and value.strip():
        try:
            n = float(value.strip())
        except ValueError:
            errors.append(f"{ctx}{field} 不是合法数字：{value!r}")
            return 0
    else:
        errors.append(f"{ctx}缺少 {field}")
        return 0
    if n < 0:
        errors.append(f"{ctx}{field} 不能为负数")
        return 0
    if n == int(n):
        return int(n)
    return n


def _first_key(obj, keys):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def normalize_table(raw) -> dict:
    """校验并归一化 JSON 故障表。

    同时兼容常见中文键名与“故障为行、检查为列”的矩阵写法。
    不合法时抛 TableValidationError（含全部错误条目）。
    """
    errors = []
    if not isinstance(raw, dict):
        raise TableValidationError(["故障表顶层必须是 JSON 对象 {}"])

    restart_raw = _first_key(raw, ["restart_seconds", "restart_time", "restart_sec",
                                   "restartDuration", "重启耗时", "重启时间"])
    restart_seconds = _as_nonneg_number(restart_raw, "restart_seconds/重启耗时",
                                        errors, "顶层：") if restart_raw is not None else 0

    faults_raw = _first_key(raw, ["faults", "fault_list", "故障", "故障列表"])
    checks_raw = _first_key(raw, ["checks", "check_list", "items", "检查", "检查项", "检查列表"])

    if not isinstance(faults_raw, list) or not faults_raw:
        errors.append("faults/故障 必须是非空数组")
        faults_raw = []
    if not isinstance(checks_raw, list) or not checks_raw:
        errors.append("checks/检查 必须是非空数组")
        checks_raw = []

    if len(faults_raw) > MAX_FAULTS:
        errors.append(f"故障数量最多 {MAX_FAULTS} 种，当前 {len(faults_raw)} 种")
    if len(checks_raw) > MAX_CHECKS:
        errors.append(f"检查项最多 {MAX_CHECKS} 项，当前 {len(checks_raw)} 项")

    # ---- faults ----
    faults = []
    seen_fid = set()
    for i, f in enumerate(faults_raw[:MAX_FAULTS] if isinstance(faults_raw, list) else []):
        ctx = f"第 {i + 1} 个故障："
        if not isinstance(f, dict):
            errors.append(ctx + "必须是对象")
            continue
        fid = _first_key(f, ["id", "fid", "code", "编号", "代码"])
        name = _first_key(f, ["name", "title", "名称", "故障名称", "故障"])
        if fid is None or str(fid).strip() == "":
            fid = f"F{i + 1}"
        fid = str(fid).strip()
        if fid in seen_fid:
            errors.append(ctx + f"故障 id 重复：{fid}")
        seen_fid.add(fid)
        if name is None or str(name).strip() == "":
            name = fid
        hint = _first_key(f, ["hint", "tip", "advice", "suggestion", "说明", "提示", "建议"]) or ""
        faults.append({"id": fid, "name": str(name).strip(), "hint": str(hint).strip()})

    # ---- checks ----
    checks = []
    seen_cid = set()
    for i, c in enumerate(checks_raw[:MAX_CHECKS] if isinstance(checks_raw, list) else []):
        ctx = f"第 {i + 1} 项检查："
        if not isinstance(c, dict):
            errors.append(ctx + "必须是对象")
            continue
        cid = _first_key(c, ["id", "cid", "code", "编号", "代码"])
        cname = _first_key(c, ["name", "title", "检查项", "名称", "项目"])
        if cid is None or str(cid).strip() == "":
            cid = f"C{i + 1}"
        cid = str(cid).strip()
        if cid in seen_cid:
            errors.append(ctx + f"检查 id 重复：{cid}")
        seen_cid.add(cid)
        if cname is None or str(cname).strip() == "":
            cname = cid

        phase_raw = _first_key(c, ["phase", "stage", "when", "阶段", "时机"])
        phase_key = canonical_reading(phase_raw) if phase_raw is not None else PHASE_PRE
        # canonical_reading 会 casefold；中文别名原样也在映射表里，故先查原值再查归一值
        phase = _PHASE_ALIASES.get(str(phase_raw).strip() if phase_raw is not None else "",
                                   _PHASE_ALIASES.get(phase_key))
        if phase is None:
            errors.append(ctx + f"阶段 {phase_raw!r} 无法识别，应为 pre/重启前 或 post/重启后")
            phase = PHASE_PRE

        sec_raw = _first_key(c, ["seconds", "duration", "cost", "time", "耗时", "检查耗时", "用时"])
        seconds = _as_nonneg_number(sec_raw, "seconds/耗时", errors, ctx) if sec_raw is not None else 0

        detail = _first_key(c, ["detail", "description", "howto", "method", "说明", "操作方法"]) or ""

        # 预期读数：优先 check.expected / 预期读数（{fid: value} 或按故障顺序的数组）
        exp_raw = _first_key(c, ["expected", "expect", "readings", "预期读数", "预期", "期望值"])
        expected = {}
        if isinstance(exp_raw, dict):
            expected = {str(k): v for k, v in exp_raw.items()}
        elif isinstance(exp_raw, list):
            for fi, f in enumerate(faults):
                if fi < len(exp_raw):
                    expected[f["id"]] = exp_raw[fi]
        else:
            expected = {}

        # 兼容“故障为行”的矩阵：faults[].readings / expected / 预期读数 中按 cid 取值
        for fraw, f in zip([x for x in faults_raw if isinstance(x, dict)], faults):
            if f["id"] in expected:
                continue
            fr = _first_key(fraw, ["readings", "expected", "expect", "预期读数", "读数"])
            if isinstance(fr, dict) and cid in fr:
                expected[f["id"]] = fr[cid]

        options_raw = _first_key(c, ["options", "choices", "values", "可选值", "选项"])
        options = []
        if isinstance(options_raw, list):
            options = [clean_reading(o) for o in options_raw if clean_reading(o)]
        # 用预期值补充下拉候选，去重保序
        norm_expected = {}
        for f in faults:
            raw_v = expected.get(f["id"])
            display_v = clean_reading(raw_v)
            if raw_v is None or display_v == "":
                errors.append(ctx + f"缺少故障 {f['id']}（{f['name']}）的预期读数")
                norm_expected[f["id"]] = ""
            else:
                norm_expected[f["id"]] = display_v
                if display_v not in options:
                    options.append(display_v)

        checks.append({
            "id": cid,
            "name": str(cname).strip(),
            "phase": phase,
            "seconds": seconds,
            "detail": str(detail).strip(),
            "options": options,
            "expected": norm_expected,
        })

    if errors:
        raise TableValidationError(errors)

    title = _first_key(raw, ["title", "name", "标题", "名称"]) or "故障排查表"
    desc = _first_key(raw, ["description", "desc", "subtitle", "说明", "描述"]) or ""
    return {
        "title": str(title).strip(),
        "description": str(desc).strip(),
        "restart_seconds": restart_seconds,
        "faults": faults,
        "checks": checks,
    }


def spent_seconds(table, readings, restarted) -> int:
    """已发生的标准耗时：已录入读数的检查耗时之和 + 已登记的重启耗时。"""
    total = table["restart_seconds"] if restarted else 0
    by_id = {c["id"]: c for c in table["checks"]}
    for cid, val in (readings or {}).items():
        if val is not None and str(val).strip() != "" and cid in by_id:
            total += by_id[cid]["seconds"]
    return total
