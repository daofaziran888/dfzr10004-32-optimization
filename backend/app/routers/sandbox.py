"""采集沙箱测试接口：维护沙箱实例与运行链，覆盖启动测试、断点重跑、关闭实例、归档实例等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sandbox import SandboxService

router = APIRouter(prefix="/api/sandbox", tags=["采集沙箱测试"])

service = SandboxService()

LIST_FIELDS = ["实例编号", "测试脚本", "目标站点", "测试参数", "预期结果", "实际结果", "测试耗时", "实例状态"]
STATUSES = ["草稿", "运行中", "已完成", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按实例编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已完成、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按实例编号与状态过滤采集沙箱测试列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集沙箱测试清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sandbox", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条沙箱实例明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"沙箱实例 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/detail", response_model=dict)
def get_detail(entry_id: int) -> dict:
    """实例运行详情：最新运行、断点定位（脚本片段、入参摘要、上轮耗时、失败断言）。"""
    detail = service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"沙箱实例 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}/runs", response_model=PageResult[dict])
def list_runs(
    entry_id: int,
    page: int = 1,
    size: int = 5,
) -> PageResult[dict]:
    """分页查看同一组沙箱参数的运行链，支持页码跳转与翻页定位。"""
    if size > 50:
        raise HTTPException(status_code=400, detail="运行历史每页最多 50 条")
    result = service.list_runs(entry_id, page=page, size=size)
    if result is None:
        raise HTTPException(status_code=404, detail=f"沙箱实例 {entry_id} 不存在或已归档")
    items, total = result
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/{entry_id}/runs", response_model=ActionResult)
def start_run(entry_id: int, payload: EntryPayload) -> ActionResult:
    """启动一轮运行：mode=fresh 从头执行，mode=resume 从断点的下一步恢复。

    同一组沙箱参数已有进行中的运行时，后提交的请求会被运行锁拦下，仅首个有效。
    """
    mode = str(payload.values.get("mode") or "fresh").strip()
    run, message = service.start_run(entry_id, mode)
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=run)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条沙箱实例，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="沙箱实例已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条沙箱实例执行启动测试、关闭实例、归档实例；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
