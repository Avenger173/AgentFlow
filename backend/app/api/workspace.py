from app.schemas.workspace import (
    WorkspaceDocumentCreateRequest,
    WorkspaceDocumentInfo,
    WorkspaceDocumentListResponse,
    WorkspaceDocumentPreviewResponse,
    WorkspaceDocumentSearchRequest,
    WorkspaceDocumentSearchResponse,
)
from app.services.workspace_documents import (
    WorkspaceDocumentError,
    get_workspace_document_preview,
    import_workspace_document_base64,
    import_workspace_document,
    list_workspace_documents,
    search_workspace_documents,
)
import asyncio

from fastapi import APIRouter, HTTPException, Query


router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/documents", response_model=WorkspaceDocumentListResponse)
async def get_workspace_documents() -> WorkspaceDocumentListResponse:
    """列出用户已导入的受控 workspace 文档。"""

    # PDF/DOCX 首次预览可能需要解析；移到线程池，避免占住 FastAPI 事件循环。
    documents = await asyncio.to_thread(list_workspace_documents)
    return WorkspaceDocumentListResponse(total=len(documents), documents=documents)


@router.post("/documents", response_model=WorkspaceDocumentInfo)
async def create_workspace_document(
    request: WorkspaceDocumentCreateRequest,
) -> WorkspaceDocumentInfo:
    """导入一份受控文本或二进制文档到 workspace。

    这里不做 multipart 上传：桌面端只提交文件名与内容，文本用 UTF-8，PDF/DOCX 用 Base64。
    因而不增加任意本机路径读取能力，也能保持 Qt 客户端协议和本地后端部署简单。
    """

    try:
        if request.content_base64 is not None:
            return await asyncio.to_thread(
                import_workspace_document_base64,
                filename=request.filename,
                content_base64=request.content_base64,
            )
        return await asyncio.to_thread(
            import_workspace_document,
            filename=request.filename,
            content=request.content or "",
        )
    except WorkspaceDocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents/{document_name}", response_model=WorkspaceDocumentPreviewResponse)
async def get_workspace_document_preview_endpoint(
    document_name: str,
    preview_chars: int = Query(default=2400, ge=0, le=8000),
) -> WorkspaceDocumentPreviewResponse:
    """读取受控 workspace 文档预览。

    路由只接受文件名，不接受任意路径；服务层会再次清洗和校验后缀。这个接口给 Qt
    或后续 Document Agent 调试面板展示“已导入材料”使用，不承担任意文件读取职责。
    """

    try:
        return await asyncio.to_thread(
            get_workspace_document_preview,
            relative_path=document_name,
            preview_chars=preview_chars,
        )
    except WorkspaceDocumentError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/search", response_model=WorkspaceDocumentSearchResponse)
async def search_workspace_documents_endpoint(
    request: WorkspaceDocumentSearchRequest,
) -> WorkspaceDocumentSearchResponse:
    """在受控 workspace 文档中做精确搜索。

    这不是任意文件 grep：API 只访问 data/workspaces 下用户显式导入的小型文本，
    作为 Document Agent 后续“先定位、再理解”的安全检索入口。
    """

    try:
        return await asyncio.to_thread(
            search_workspace_documents,
            query=request.query,
            limit=request.limit,
            case_sensitive=request.case_sensitive,
            context_chars=request.context_chars,
        )
    except WorkspaceDocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
