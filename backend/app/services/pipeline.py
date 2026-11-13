"""采集链路监控业务规则：状态流转、字段校验与筛选口径都收在这里。

沙箱覆盖与验证状态不落地冗余字段：读取清单时从沙箱实例的「覆盖节点」与
沙箱运行链实时汇总，只有完整收口（验证通过/验证失败）的运行才参与判定，
结果冲突时以最近一次完整运行的结论为准；未收口的运行不覆盖既有结论。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "pipeline"
REQUIRED_FIELDS = ["节点编号", "节点名称", "上游节点"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已归档"]
ACTION_RULES = {"启动节点": "草稿", "暂停节点": "运行中", "归档节点": "已暂停"}
NEGATIVE_ACTIONS = []

SETTLED_RUN_STATUSES = ("验证通过", "验证失败")


def sandbox_node_status() -> dict[str, dict[str, Any]]:
    """节点编号 -> 沙箱覆盖、验证状态、最近验证运行。

    完整运行按完成时间升序遍历，后写入的覆盖先写入的，保证最近一次完整运行的结论生效。
    """
    instances = store.rows("sandbox")
    coverage: dict[str, dict[str, Any]] = {}
    for instance in instances:
        for node in instance.get("覆盖节点") or []:
            coverage.setdefault(str(node), {"沙箱覆盖": "已覆盖", "验证状态": "未验证",
                                            "最近验证运行": None})
    instance_by_id = {int(item.get("id", 0)): item for item in instances}
    settled = [run for run in store.rows("sandbox_run")
               if run.get("status") in SETTLED_RUN_STATUSES]
    settled.sort(key=lambda run: str(run.get("finished_at") or ""))
    for run in settled:
        instance = instance_by_id.get(int(run.get("instance_id") or 0))
        if instance is None:
            continue
        for node in instance.get("覆盖节点") or []:
            coverage[str(node)] = {"沙箱覆盖": "已覆盖", "验证状态": run["status"],
                                   "最近验证运行": run.get("run_no")}
    return coverage


class PipelineService:
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
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_sandbox_status(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._with_sandbox_status(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"链路节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集链路监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"链路节点已{action}"

    @staticmethod
    def _with_sandbox_status(row: dict[str, Any]) -> dict[str, Any]:
        """把沙箱覆盖与验证状态合入节点行，不改动存储里的原始记录。"""
        merged = dict(row)
        status = sandbox_node_status().get(str(row.get("节点编号") or ""))
        merged["沙箱覆盖"] = status["沙箱覆盖"] if status else "未覆盖"
        merged["验证状态"] = status["验证状态"] if status else "未验证"
        merged["最近验证运行"] = (status["最近验证运行"] or "—") if status else "—"
        return merged
