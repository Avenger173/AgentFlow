from __future__ import annotations

from app.database.memory_repository import (
    LongTermMemoryNotFoundError,
    clear_long_term_memories,
    create_long_term_memory,
    delete_long_term_memory,
    get_long_term_memory,
    list_long_term_memories,
    update_long_term_memory,
)
from app.schemas.memory import (
    LongTermMemoryClearResponse,
    LongTermMemoryCreateRequest,
    LongTermMemoryListResponse,
    LongTermMemoryRecord,
    LongTermMemoryUpdateRequest,
)
from app.services.long_term_memory import (
    LongTermMemorySafetyError,
    normalize_memory_scope,
    normalize_memory_source_task_id,
    normalize_memory_tags,
    sanitize_memory_text,
)
from fastapi import APIRouter, HTTPException, Query, Response, status


router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("", response_model=LongTermMemoryListResponse)
def list_memories(
    scope: str | None = Query(default=None, max_length=80),
    include_disabled: bool = True,
) -> LongTermMemoryListResponse:
    """查看当前用户已确认的长期记忆，永不返回原始对话、文件正文或密钥。"""

    try:
        normalized_scope = normalize_memory_scope(scope) if scope else None
        items = list_long_term_memories(scope=normalized_scope, include_disabled=include_disabled)
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LongTermMemoryListResponse(items=items, total=len(items))


@router.post("", response_model=LongTermMemoryRecord, status_code=status.HTTP_201_CREATED)
def create_memory(request: LongTermMemoryCreateRequest) -> LongTermMemoryRecord:
    """创建用户已确认的短记忆；后台不会从模型回答自动调用此接口。"""

    if not request.user_confirmed:
        raise HTTPException(status_code=400, detail="保存长期记忆需要用户明确确认。")
    try:
        return create_long_term_memory(
            kind=request.kind,
            scope=normalize_memory_scope(request.scope),
            title=sanitize_memory_text(request.title, field_name="记忆标题", maximum=120),
            summary=sanitize_memory_text(request.summary, field_name="记忆摘要", maximum=1000),
            tags=normalize_memory_tags(request.tags),
            source_task_id=normalize_memory_source_task_id(request.source_task_id),
            user_confirmed=True,
        )
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{memory_id}", response_model=LongTermMemoryRecord)
def get_memory(memory_id: str) -> LongTermMemoryRecord:
    try:
        return get_long_term_memory(memory_id)
    except LongTermMemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{memory_id}", response_model=LongTermMemoryRecord)
def update_memory(memory_id: str, request: LongTermMemoryUpdateRequest) -> LongTermMemoryRecord:
    try:
        return update_long_term_memory(
            memory_id,
            title=(
                sanitize_memory_text(request.title, field_name="记忆标题", maximum=120)
                if request.title is not None
                else None
            ),
            summary=(
                sanitize_memory_text(request.summary, field_name="记忆摘要", maximum=1000)
                if request.summary is not None
                else None
            ),
            tags=normalize_memory_tags(request.tags) if request.tags is not None else None,
            enabled=request.enabled,
        )
    except LongTermMemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: str) -> Response:
    try:
        delete_long_term_memory(memory_id)
    except LongTermMemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", response_model=LongTermMemoryClearResponse)
def clear_memories(
    scope: str = Query(default="global", max_length=80),
    confirm: bool = Query(default=False),
) -> LongTermMemoryClearResponse:
    """仅按明确范围清空；没有 confirm=true 时拒绝，避免设置页误触造成不可逆删除。"""

    if not confirm:
        raise HTTPException(status_code=400, detail="清空长期记忆需要 confirm=true 明确确认。")
    try:
        normalized_scope = normalize_memory_scope(scope)
        deleted_count = clear_long_term_memories(normalized_scope)
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LongTermMemoryClearResponse(scope=normalized_scope, deleted_count=deleted_count)
