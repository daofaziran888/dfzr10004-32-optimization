"""采集沙箱测试业务规则：实例生命周期、运行链、断点重跑与跨模块同步。

口径约定：
- 运行按（测试脚本、目标站点、测试参数）的指纹串成运行链，次数在链上递增；
  同一组参数同时只允许一个进行中的运行（运行锁），后提交的直接拒绝。
- 断点粒度为步骤级：重跑从断点的下一步恢复，之前的步骤沿用上轮结果。
- 套接字超时、步骤崩溃、挂起被关闭的运行都是「未收口」，永远不能被记作验证通过，
  也不进入采集结果校验；只有实际结果与预期结果都有效的运行才会同步到校验模块。
- 旧运行每组参数保留最近 10 次，超出部分清理；结果冲突时以最近一次完整运行的结论为准。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services import sandbox_engine as engine
from app.store import store

MODULE = "sandbox"
RUN_TABLE = "sandbox_run"
REQUIRED_FIELDS = ["实例编号", "测试脚本", "目标站点"]
OPTIONAL_FIELDS = ["测试参数", "预期结果", "覆盖节点"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已关闭"]
# 动作: (允许的当前状态, 目标状态)
ACTION_RULES = {"启动测试": ("草稿", "运行中"), "关闭实例": ("运行中", "已关闭"), "归档实例": ("已完成", "已关闭")}
# 每组参数保留的运行历史上限（断点粒度与差异比较方式见 sandbox_engine）
RUN_RETENTION = 10


class SandboxService:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._run_locks: dict[str, str] = {}  # param_key -> 进行中的 run_no

    # ------------------------------------------------------------------ 实例
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry.setdefault("测试参数", "")
        entry.setdefault("预期结果", "")
        entry.setdefault("覆盖节点", [])
        entry["实际结果"] = None
        entry["测试耗时"] = None
        entry["实例状态"] = "未运行"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"沙箱实例 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集沙箱测试可执行范围"
        source, target = ACTION_RULES[action]
        if entry.get("status") != source:
            return None, f"当前状态为「{entry.get('status')}」，不能执行「{action}」"
        if action == "启动测试":
            run, message = self.start_run(entry_id, mode="fresh")
            if run is None:
                return None, message
            return entry, f"沙箱实例已启动测试，运行 {run['run_no']}：{run['status']}"
        if action == "关闭实例":
            self._force_settle_active_runs(entry)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        return entry, f"沙箱实例已{action}"

    # ------------------------------------------------------------------ 运行
    def start_run(self, entry_id: int, mode: str) -> tuple[dict[str, Any] | None, str]:
        """启动一轮运行。mode=fresh 从头执行；mode=resume 从断点的下一步恢复。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"沙箱实例 {entry_id} 不存在或已归档"
        if entry.get("status") == "已关闭":
            return None, "实例已关闭，不能启动运行"
        if mode not in ("fresh", "resume"):
            return None, f"运行方式「{mode}」不支持，可选 fresh / resume"

        key = engine.param_key(entry.get("测试脚本"), entry.get("目标站点"), entry.get("测试参数"))
        with self._guard:
            holder = self._run_locks.get(key)
            if holder is not None:
                return None, f"同一组沙箱参数已有运行 {holder} 在进行，仅首个提交有效"
            chain = self._runs_for(key)
            prev_run = chain[0] if chain else None
            if mode == "resume":
                if prev_run is None or prev_run.get("status") not in (
                        engine.RUN_STATUS_FAILED, engine.RUN_STATUS_UNSETTLED):
                    return None, "最近一次运行没有可恢复的断点，请从头重跑"
                total_steps = len(engine.parse_script(entry.get("测试脚本", "")))
                if int(prev_run.get("breakpoint_step") or 0) >= total_steps:
                    return None, "断点已在脚本末尾，没有可恢复的下一步"
            attempt = (int(prev_run["attempt"]) if prev_run else 0) + 1
            run_no = self._next_run_no()
            self._run_locks[key] = run_no
        try:
            run = engine.execute_run(entry=entry, attempt=attempt, mode=mode,
                                     prev_run=prev_run, run_no=run_no)
            run["id"] = self._next_run_id()
            store.rows(RUN_TABLE).append(run)
            self._prune_runs(key)
            if run["status"] != engine.RUN_STATUS_RUNNING:
                self._run_locks.pop(key, None)
                self._settle(entry, run, prev_run)
            else:
                # 挂起的运行：实例保持运行中，运行锁继续持有
                entry["status"] = "运行中"
                entry["pending"] = True
        except Exception:
            self._run_locks.pop(key, None)
            raise
        return run, f"运行 {run_no} 已结束：{run['status']}" if run["status"] != engine.RUN_STATUS_RUNNING \
            else f"运行 {run_no} 进行中"

    def list_runs(self, entry_id: int, *, page: int = 1, size: int = 20
                  ) -> tuple[list[dict[str, Any]], int] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        key = engine.param_key(entry.get("测试脚本"), entry.get("目标站点"), entry.get("测试参数"))
        chain = self._runs_for(key)
        total = len(chain)
        start = max(page - 1, 0) * size
        return chain[start:start + size], total

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """实例详情：最新运行、断点定位（脚本片段、入参摘要、上轮耗时、失败断言）。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        key = engine.param_key(entry.get("测试脚本"), entry.get("目标站点"), entry.get("测试参数"))
        chain = self._runs_for(key)
        latest = chain[0] if chain else None
        return {
            "entry": entry,
            "latest_run": latest,
            "breakpoint": self._build_breakpoint(entry, latest, chain),
            "run_total": len(chain),
            "rerun_count": (int(latest["attempt"]) - 1) if latest else 0,
            "lock_holder": self._run_locks.get(key),
        }

    # ------------------------------------------------------------------ 内部
    def _runs_for(self, key: str) -> list[dict[str, Any]]:
        """同一组参数的运行链，按运行次数倒序（最新在前）。"""
        runs = [run for run in store.rows(RUN_TABLE) if run.get("param_key") == key]
        runs.sort(key=lambda run: int(run.get("attempt", 0)), reverse=True)
        return runs

    def _next_run_id(self) -> int:
        return max((int(run.get("id", 0)) for run in store.rows(RUN_TABLE)), default=0) + 1

    def _next_run_no(self) -> str:
        return f"RUN-{self._next_run_id():04d}"

    def _prune_runs(self, key: str) -> None:
        """旧运行保留策略：每组参数只保留最近 RUN_RETENTION 次，更早的清理掉。"""
        rows = store.rows(RUN_TABLE)
        chain = [run for run in rows if run.get("param_key") == key]
        chain.sort(key=lambda run: int(run.get("attempt", 0)), reverse=True)
        stale = {id(run) for run in chain[RUN_RETENTION:]}
        if stale:
            rows[:] = [run for run in rows if id(run) not in stale]

    def _build_breakpoint(self, entry: dict[str, Any], latest: dict[str, Any] | None,
                          chain: list[dict[str, Any]]) -> dict[str, Any] | None:
        """断点定位：再次打开实例时直接给出脚本片段、入参摘要、上轮耗时与失败断言。"""
        if latest is None or latest.get("status") == engine.RUN_STATUS_PASSED:
            return None
        failed_step = latest.get("failed_step")
        if failed_step is None and latest.get("status") not in (
                engine.RUN_STATUS_UNSETTLED, engine.RUN_STATUS_RUNNING):
            return None
        script_lines = [
            {"seq": step.seq, "text": step.raw,
             "是否断点": step.seq == int(latest.get("breakpoint_step") or 0),
             "是否失败": step.seq == failed_step}
            for step in engine.parse_script(entry.get("测试脚本", ""))
        ]
        position = next((index + 1 for index, run in enumerate(chain)
                         if run.get("run_no") == latest.get("run_no")), 1)
        return {
            "run_no": latest.get("run_no"),
            "运行状态": latest.get("status"),
            "断点步骤": latest.get("breakpoint_step"),
            "恢复步骤": int(latest.get("breakpoint_step") or 0) + 1,
            "失败步骤": failed_step,
            "脚本片段": next((line["text"] for line in script_lines if line["是否失败"]), None),
            "脚本行": script_lines,
            "入参摘要": latest.get("入参摘要"),
            "上轮耗时ms": int(latest.get("elapsed_ms") or 0) + int(latest.get("carried_ms") or 0),
            "失败断言": latest.get("failed_assertion"),
            "错误信息": latest.get("错误信息"),
            "历史序号": position,
        }

    def _settle(self, entry: dict[str, Any], run: dict[str, Any],
                prev_run: dict[str, Any] | None) -> None:
        """运行收口：回写实例、生成差异摘要、同步校验模块、写审计。"""
        if prev_run is not None and run["status"] in engine.SETTLED_STATUSES:
            run["差异摘要"] = engine.build_diff(prev_run, run)
        entry["实际结果"] = run.get("实际结果")
        entry["测试耗时"] = f"{int(run.get('elapsed_ms') or 0) + int(run.get('carried_ms') or 0)}ms"
        entry["实例状态"] = run["status"]
        entry["abnormal"] = run["status"] != engine.RUN_STATUS_PASSED
        if run["status"] in engine.SETTLED_STATUSES:
            entry["status"] = "已完成"
            entry["pending"] = False
        elif run["status"] == engine.RUN_STATUS_UNSETTLED:
            # 未收口：实例停在运行中，不能被记作验证通过
            entry["status"] = "运行中"
            entry["pending"] = True
        # 校验门禁：只有实际结果与预期结果都有效的运行才能进入采集结果校验
        if run.get("result_valid"):
            self._sync_verify(entry, run)
        self._write_audit(entry, run)

    def _force_settle_active_runs(self, entry: dict[str, Any]) -> None:
        """关闭实例时，进行中的运行强制停在未收口，释放运行锁，且不算验证通过。"""
        key = engine.param_key(entry.get("测试脚本"), entry.get("目标站点"), entry.get("测试参数"))
        for run in self._runs_for(key):
            if run.get("status") != engine.RUN_STATUS_RUNNING:
                continue
            run["status"] = engine.RUN_STATUS_UNSETTLED
            run["错误信息"] = "实例关闭，挂起的运行强制收口"
            run["finished_at"] = engine.now_text()
            run["实际结果"] = None
            run["result_valid"] = False
            self._run_locks.pop(key, None)
            self._settle(entry, run, None)

    def _sync_verify(self, entry: dict[str, Any], run: dict[str, Any]) -> None:
        """把运行结论同步进采集结果校验；同一实例只留一条，最近一次完整运行覆盖旧结论。"""
        rows = store.rows("verify")
        target = next((row for row in rows
                       if row.get("校验对象") == entry.get("实例编号")
                       and row.get("校验类型") == "沙箱运行结论"), None)
        if target is None:
            target = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            target["规则编号"] = f"VERI-{int(target['id']):04d}"
            rows.append(target)
        target.update({
            "校验对象": entry.get("实例编号"),
            "校验类型": "沙箱运行结论",
            "校验表达式": entry.get("预期结果"),
            "阈值配置": run.get("入参摘要"),
            "采样比例": "100%",
            "阻断级别": "阻断" if run["status"] == engine.RUN_STATUS_FAILED else "提示",
            "最近校验": f"{run['run_no']} {run['status']}（第{run['attempt']}次运行 {run.get('finished_at')}）",
            "status": "运行中",
            "pending": True,
            "abnormal": run["status"] != engine.RUN_STATUS_PASSED,
        })

    def _write_audit(self, entry: dict[str, Any], run: dict[str, Any]) -> None:
        """每次运行收口都写审计记录，结果摘要里保留累计重跑次数。"""
        rows = store.rows("audit")
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        attempt = int(run.get("attempt") or 1)
        rerun_count = attempt - 1
        diff = run.get("差异摘要")
        rows.append({
            "id": entry_id,
            "记录编号": f"AUDI-{entry_id:04d}",
            "操作类型": "沙箱首跑" if attempt == 1 else "沙箱重跑",
            "采集计划": entry.get("实例编号"),
            "执行人": "沙箱调度",
            "执行时间": run.get("finished_at") or engine.now_text(),
            "结果摘要": f"{run['run_no']} {run['status']}；累计运行{attempt}次，重跑{rerun_count}次",
            "变更内容": diff["文本摘要"] if diff else "首次运行，无对比基线",
            "复核状态": "待复核",
            "重跑次数": rerun_count,
            "status": "已记录",
            "pending": True,
            "abnormal": run["status"] != engine.RUN_STATUS_PASSED,
        })
