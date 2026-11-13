"""沙箱运行引擎：把测试脚本拆成步骤执行，记录断点、耗时与实际结果。

设计约定（断点粒度、差异比较、保留策略都由这里收口）：
- 断点粒度：步骤级。脚本每行一个步骤，断点 = 最后一次完整运行中已完成的步骤序号，
  重跑从断点的下一步恢复，断点之前的步骤沿用上轮结果，不重复执行。
- 目标站点响应是确定性模拟：同一组（站点、参数、运行次数）必得同一上下文，
  运行次数越高返回的数据越完整，用来演示"重跑后断言通过"的改善路径。
- 测试参数里可注入故障：fault_type = socket_timeout / crash / hang，fault_step = 第几步。
  套接字超时与步骤崩溃让运行停在「未收口」；hang 让运行停在「运行中」并持有运行锁。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

RUN_STATUS_RUNNING = "运行中"
RUN_STATUS_PASSED = "验证通过"
RUN_STATUS_FAILED = "验证失败"
RUN_STATUS_UNSETTLED = "未收口"
SETTLED_STATUSES = (RUN_STATUS_PASSED, RUN_STATUS_FAILED)

ASSERT_PREFIX = "assert "


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class StepDef:
    seq: int
    name: str
    assertion: str | None
    raw: str


def parse_script(script: str) -> list[StepDef]:
    """把多行脚本解析成步骤：每行「步骤名 | assert 表达式」，没有竖线则只记步骤名。"""
    steps: list[StepDef] = []
    for line in str(script or "").splitlines():
        text = line.strip()
        if not text:
            continue
        name, _, rest = text.partition("|")
        rest = rest.strip()
        assertion = rest[len(ASSERT_PREFIX):].strip() if rest.startswith(ASSERT_PREFIX) else None
        steps.append(StepDef(seq=len(steps) + 1, name=name.strip() or f"步骤{len(steps) + 1}",
                             assertion=assertion, raw=text))
    return steps


def parse_params(raw: Any) -> dict[str, Any]:
    """测试参数支持 JSON 对象或 k=v&k=v 形式；解析失败时返回空 dict，由入参摘要原样展示。"""
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    params: dict[str, Any] = {}
    for chunk in re.split(r"[&;]", text):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            params[key.strip()] = value.strip()
    return params


def summarize_params(params: dict[str, Any]) -> str:
    return "、".join(f"{key}={value}" for key, value in params.items()) or "无参数"


def param_key(script: Any, site: Any, params_raw: Any) -> str:
    """运行锁与运行链都按（测试脚本、目标站点、测试参数）归一化后的指纹来判同。"""
    params = parse_params(params_raw)
    normalized = json.dumps(
        {"script": str(script or "").strip(), "site": str(site or "").strip(),
         "params": {k: params[k] for k in sorted(params)}},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _digest(*parts: Any) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)


def simulate_fetch(site: str, params: dict[str, Any], attempt: int) -> dict[str, Any]:
    """模拟目标站点响应：重跑次数越高，返回的条目越全（演示断点重跑后的改善）。"""
    digest = _digest("fetch", site, json.dumps(params, sort_keys=True, ensure_ascii=False), attempt)
    return {
        "status_code": 200,
        "items_count": 36 + digest % 8 + (attempt - 1) * 16,
        "fields": ["title", "price", "url"],
        "elapsed_ms": 80 + (digest >> 8) % 160,
    }


def step_duration(site: str, params: dict[str, Any], attempt: int, seq: int) -> int:
    """每步耗时同样是确定性的：同一轮重跑结果可复现，不同轮次可比较。"""
    return 90 + _digest("duration", site, json.dumps(params, sort_keys=True, ensure_ascii=False),
                        attempt, seq) % 320


_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.In, ast.NotIn,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
)


def _assertion_names(tree: ast.AST) -> list[str]:
    return sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


def eval_assertion(expr: str, context: dict[str, Any]) -> tuple[bool | None, str | None, list[str]]:
    """用 AST 白名单求值断言，返回（结果， 错误信息， 引用到的变量名）。

    结果 None 表示表达式本身有问题（语法或未知变量），按步骤崩溃处理。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return None, f"断言语法错误：{exc.msg}", []
    names = _assertion_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_EXPR_NODES):
            return None, f"断言含有不支持的语法：{type(node).__name__}", names
    unknown = [name for name in names if name not in context]
    if unknown:
        return None, f"断言引用了未知变量：{'、'.join(unknown)}", names
    result = bool(eval(compile(tree, "<assert>", "eval"), {"__builtins__": {}}, dict(context)))
    return result, None, names


class StepFault(Exception):
    """步骤级故障：socket_timeout（套接字超时）或 crash（步骤崩溃），运行停在未收口。"""

    def __init__(self, kind: str, seq: int) -> None:
        super().__init__(kind)
        self.kind = kind
        self.seq = seq


class RunSuspended(Exception):
    """fault_type=hang：运行挂起，保持「运行中」并继续持有运行锁。"""


def _fault_at(params: dict[str, Any], seq: int) -> str | None:
    fault_step = params.get("fault_step")
    if fault_step is None or str(fault_step) != str(seq):
        return None
    kind = str(params.get("fault_type") or "").strip()
    return kind or None


def execute_run(
    *,
    entry: dict[str, Any],
    attempt: int,
    mode: str,
    prev_run: dict[str, Any] | None,
    run_no: str,
) -> dict[str, Any]:
    """执行一轮沙箱运行。mode=resume 时从上一轮断点的下一步恢复，其余步骤沿用上轮结果。"""
    script = str(entry.get("测试脚本") or "")
    site = str(entry.get("目标站点") or "")
    params = parse_params(entry.get("测试参数"))
    step_defs = parse_script(script)
    # 上下文 = 测试参数（入参）+ 模拟抓取结果（实际值），断言两边都能引用
    fetch = simulate_fetch(site, params, attempt)
    context = {**params, **fetch}

    resume_from = 1
    carried_steps: list[dict[str, Any]] = []
    if mode == "resume" and prev_run is not None:
        resume_from = int(prev_run.get("breakpoint_step") or 0) + 1
        carried_steps = [dict(step, **{"来源": "上轮沿用"})
                         for step in prev_run.get("steps", []) if int(step.get("seq", 0)) < resume_from]

    run: dict[str, Any] = {
        "run_no": run_no,
        "instance_id": entry.get("id"),
        "实例编号": entry.get("实例编号"),
        "param_key": param_key(entry.get("测试脚本"), entry.get("目标站点"), entry.get("测试参数")),
        "测试脚本": script,
        "目标站点": site,
        "测试参数": entry.get("测试参数"),
        "attempt": attempt,
        "触发方式": {"fresh": "从头运行", "resume": "断点重跑"}.get(mode, mode),
        "status": RUN_STATUS_RUNNING,
        "steps": carried_steps,
        "breakpoint_step": resume_from - 1,
        "failed_step": None,
        "failed_assertion": None,
        "错误信息": None,
        "入参摘要": summarize_params(params),
        "elapsed_ms": 0,
        "carried_ms": sum(int(step.get("耗时ms") or 0) for step in carried_steps),
        "实际结果": None,
        "预期结果": entry.get("预期结果"),
        "result_valid": False,
        "差异摘要": None,
        "started_at": now_text(),
        "finished_at": None,
    }

    try:
        for step_def in step_defs:
            if step_def.seq < resume_from:
                continue
            fault = _fault_at(params, step_def.seq)
            if fault == "hang":
                raise RunSuspended()
            if fault in ("socket_timeout", "crash"):
                raise StepFault(fault, step_def.seq)
            run["steps"].append(_execute_step(step_def, context, site, params, attempt))
            last = run["steps"][-1]
            run["elapsed_ms"] += int(last["耗时ms"])
            if last["状态"] == "崩溃":
                run["status"] = RUN_STATUS_UNSETTLED
                run["failed_step"] = step_def.seq
                run["错误信息"] = last["错误"]
                run["breakpoint_step"] = step_def.seq - 1
                break
            if last["状态"] == "失败":
                run["status"] = RUN_STATUS_FAILED
                run["failed_step"] = step_def.seq
                run["failed_assertion"] = (
                    f"{ASSERT_PREFIX}{step_def.assertion}（实际 {last['实际值']}）"
                    if step_def.assertion else f"步骤「{step_def.name}」未通过"
                )
                run["breakpoint_step"] = step_def.seq - 1
                break
            run["breakpoint_step"] = step_def.seq
        else:
            run["status"] = RUN_STATUS_PASSED
    except StepFault as fault:
        seq = fault.seq
        step_def = step_defs[seq - 1] if 0 < seq <= len(step_defs) else None
        run["status"] = RUN_STATUS_UNSETTLED
        run["failed_step"] = seq
        run["breakpoint_step"] = seq - 1
        run["错误信息"] = ("套接字超时：目标站点 5s 未响应" if fault.kind == "socket_timeout"
                        else "步骤执行崩溃：工作进程异常退出")
        if step_def is not None:
            run["steps"].append({
                "seq": seq, "名称": step_def.name, "脚本": step_def.raw, "断言": step_def.assertion,
                "状态": "超时" if fault.kind == "socket_timeout" else "崩溃",
                "耗时ms": 0, "实际值": "—", "来源": "本次执行", "错误": run["错误信息"],
            })
    except RunSuspended:
        # 保持运行中：不收口、不释放锁，等待关闭实例强制收口
        run["错误信息"] = "运行挂起：等待目标站点响应"

    pending_seqs = {step["seq"] for step in run["steps"]}
    for step_def in step_defs:
        if step_def.seq not in pending_seqs:
            run["steps"].append({
                "seq": step_def.seq, "名称": step_def.name, "脚本": step_def.raw,
                "断言": step_def.assertion, "状态": "未执行", "耗时ms": 0,
                "实际值": "—", "来源": "本次执行", "错误": None,
            })
    run["steps"].sort(key=lambda step: int(step["seq"]))

    if run["status"] in SETTLED_STATUSES:
        run["finished_at"] = now_text()
        run["实际结果"] = json.dumps(fetch, ensure_ascii=False)
        run["result_valid"] = bool(run["实际结果"]) and bool(str(run.get("预期结果") or "").strip())
    elif run["status"] == RUN_STATUS_UNSETTLED:
        # 未收口：实际结果不完整，明确标记无效，不能进入采集结果校验
        run["finished_at"] = now_text()
        run["实际结果"] = None
        run["result_valid"] = False
    return run


def _execute_step(step_def: StepDef, context: dict[str, Any], site: str,
                  params: dict[str, Any], attempt: int) -> dict[str, Any]:
    duration = step_duration(site, params, attempt, step_def.seq)
    step: dict[str, Any] = {
        "seq": step_def.seq, "名称": step_def.name, "脚本": step_def.raw,
        "断言": step_def.assertion, "耗时ms": duration, "来源": "本次执行", "错误": None,
    }
    if not step_def.assertion:
        step.update({"状态": "通过", "实际值": "—"})
        return step
    ok, error, names = eval_assertion(step_def.assertion, context)
    step["实际值"] = "、".join(f"{name}={context[name]}" for name in names if name in context) or "—"
    if error is not None:
        step.update({"状态": "崩溃", "错误": f"步骤执行崩溃：{error}"})
    elif ok:
        step["状态"] = "通过"
    else:
        step["状态"] = "失败"
    return step


def build_diff(prev_run: dict[str, Any], cur_run: dict[str, Any]) -> dict[str, Any]:
    """重跑完成后与上一版运行逐步对比：结论变化、耗时变化、步骤状态变化。"""
    prev_steps = {int(step["seq"]): step for step in prev_run.get("steps", [])}
    changes: list[dict[str, Any]] = []
    for step in cur_run.get("steps", []):
        seq = int(step["seq"])
        prev_step = prev_steps.get(seq)
        if prev_step is None or prev_step.get("状态") == step.get("状态"):
            continue
        if step.get("状态") == "通过":
            kind = "已修复"
        elif prev_step.get("状态") == "通过":
            kind = "新增失败"
        else:
            kind = "状态变化"
        changes.append({"seq": seq, "名称": step.get("名称"),
                        "上轮": prev_step.get("状态"), "本轮": step.get("状态"), "变化": kind})

    prev_total = int(prev_run.get("elapsed_ms") or 0) + int(prev_run.get("carried_ms") or 0)
    cur_total = int(cur_run.get("elapsed_ms") or 0) + int(cur_run.get("carried_ms") or 0)
    delta = cur_total - prev_total
    rate = f"{delta / prev_total * 100:+.1f}%" if prev_total else "—"
    conclusion = (f"{prev_run['status']} → {cur_run['status']}"
                  if prev_run["status"] != cur_run["status"] else f"保持{cur_run['status']}")

    fixed = [f"第{change['seq']}步" for change in changes if change["变化"] == "已修复"]
    broken = [f"第{change['seq']}步" for change in changes if change["变化"] == "新增失败"]
    parts = [f"结论{conclusion}"]
    if fixed:
        parts.append(f"已修复：{'、'.join(fixed)}")
    if broken:
        parts.append(f"新增失败：{'、'.join(broken)}")
    if not changes:
        parts.append("步骤结果无变化")
    parts.append(f"总耗时 {prev_total}ms → {cur_total}ms（{rate}）")

    return {
        "对比运行": prev_run.get("run_no"),
        "结论变化": conclusion,
        "耗时变化": {"上轮ms": prev_total, "本轮ms": cur_total, "差值ms": delta, "变化率": rate},
        "步骤变化": changes,
        "文本摘要": "；".join(parts),
    }
