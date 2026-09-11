from __future__ import annotations

from app.database.memory_repository import (
    LongTermMemoryNotFoundError,
    LongTermMemoryProposalNotFoundError,
    LongTermMemoryProposalStateError,
    clear_long_term_memories,
    confirm_long_term_memory_proposal,
    create_long_term_memory,
    delete_long_term_memory,
    get_long_term_memory,
    get_long_term_memory_proposal,
    list_long_term_memories,
    list_long_term_memory_proposals,
    reject_long_term_memory_proposal,
    update_long_term_memory,
)
from app.database.memory_observability_repository import list_memory_observations
from app.schemas.memory import (
    LongTermMemoryClearResponse,
    LongTermMemoryCreateRequest,
    LongTermMemoryListResponse,
    MemoryObservationListResponse,
    LongTermMemoryProposal,
    LongTermMemoryProposalConfirmRequest,
    LongTermMemoryProposalListResponse,
    LongTermMemoryProposalRejectRequest,
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


@router.get("/observations", response_model=MemoryObservationListResponse)
def list_observations(limit: int = Query(default=100, ge=1, le=200)) -> MemoryObservationListResponse:
    """返回最近无正文记忆观测，帮助本地诊断且不暴露客户内容。"""

    return MemoryObservationListResponse(items=list_memory_observations(limit=limit))


@router.get("/proposals", response_model=LongTermMemoryProposalListResponse)
def list_memory_proposals(
    scope: str | None = Query(default=None, max_length=80),
) -> LongTermMemoryProposalListResponse:
    """列出当前设置范围的待确认候选；它们尚未参与跨会话检索。"""

    try:
        normalized_scope = normalize_memory_scope(scope) if scope else None
        items = list_long_term_memory_proposals(scope=normalized_scope, statuses={"pending"})
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LongTermMemoryProposalListResponse(
        scope=normalized_scope,
        items=items,
        note="当前没有待确认的长期记忆候选。" if not items else "候选尚未写入长期记忆，可编辑后确认或拒绝。",
    )


@router.post("/proposals/{proposal_id}/confirm", response_model=LongTermMemoryRecord)
def confirm_memory_proposal(
    proposal_id: str,
    request: LongTermMemoryProposalConfirmRequest,
) -> LongTermMemoryRecord:
    """从设置页确认候选，不要求其来源任务仍处于客户可见的完成态。"""

    if proposal_id != request.proposal_id:
        raise HTTPException(status_code=400, detail="URL 与请求体中的候选 ID 不一致。")
    if not request.user_confirmed:
        raise HTTPException(status_code=400, detail="保存长期记忆需要用户明确确认。")
    try:
        get_long_term_memory_proposal(proposal_id)
        _, record = confirm_long_term_memory_proposal(
            proposal_id=proposal_id,
            kind=request.kind,
            scope=normalize_memory_scope(request.scope),
            title=sanitize_memory_text(request.title, field_name="记忆标题", maximum=120),
            summary=sanitize_memory_text(request.summary, field_name="记忆摘要", maximum=1000),
            tags=normalize_memory_tags(request.tags),
        )
        return record
    except LongTermMemoryProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LongTermMemoryProposalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/reject", response_model=LongTermMemoryProposal)
def reject_memory_proposal(
    proposal_id: str,
    request: LongTermMemoryProposalRejectRequest,
) -> LongTermMemoryProposal:
    """从设置页显式拒绝候选；同一请求重试只返回原拒绝记录。"""

    if not request.user_rejected:
        raise HTTPException(status_code=400, detail="拒绝长期记忆候选需要 user_rejected=true 明确确认。")
    try:
        return reject_long_term_memory_proposal(proposal_id)
    except LongTermMemoryProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LongTermMemoryProposalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
