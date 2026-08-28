from typing import Literal

from pydantic import BaseModel, Field, model_validator


class WorkspaceDocumentCreateRequest(BaseModel):
    """导入受控 workspace 文档的请求体。

    当前桌面端会读取用户选择的文件内容，再通过 JSON 交给后端写入 data/workspaces。
    ``content`` 保持原有 UTF-8 文本协议；PDF/DOCX/图片使用 ``content_base64`` 承载原始字节。
    两者必须二选一，后端始终不接收任意本机路径。
    """

    filename: str = Field(min_length=1)
    content: str | None = None
    # 10 MB 原始二进制编码为 Base64 后约 13.34 MB，14 MB 是协议层硬上限，真正文件大小仍
    # 会在服务层按格式再次校验，避免巨大 JSON 请求在解析器前消耗内存。
    content_base64: str | None = Field(default=None, max_length=14_000_000)

    @model_validator(mode="after")
    def validate_content_transport(self) -> "WorkspaceDocumentCreateRequest":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("content 与 content_base64 必须且只能提供一个。")
        return self


class WorkspaceDocumentInfo(BaseModel):
    name: str
    relative_path: str
    size_bytes: int
    modified_at: str
    document_type: Literal["text", "pdf", "docx", "image"] = "text"
    preview: str = ""


class WorkspaceDocumentListResponse(BaseModel):
    total: int
    documents: list[WorkspaceDocumentInfo] = Field(default_factory=list)


class WorkspaceDocumentPreviewResponse(BaseModel):
    """受控 workspace 文档预览响应。

    预览接口只返回工作区相对文件名和受限长度文本，不暴露后端绝对路径；Runtime 内部
    需要绝对路径时继续走自己的受控读取函数。
    """

    name: str
    relative_path: str
    size_bytes: int
    modified_at: str
    document_type: Literal["text", "pdf", "docx", "image"] = "text"
    preview_chars: int
    truncated: bool = False
    preview: str = ""


class WorkspaceDocumentSearchRequest(BaseModel):
    """受控 workspace 文档精确搜索请求。

    这是后续 agentic search 的轻量入口：先用确定性文本匹配快速定位文档行，
    需要语义理解时再把命中的片段交给 LLM 或未来 RAG，而不是一开始就全量向量化。
    """

    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)
    case_sensitive: bool = False
    context_chars: int = Field(default=80, ge=0, le=240)


class WorkspaceDocumentSearchMatch(BaseModel):
    document_name: str
    relative_path: str
    line_number: int
    line_text: str
    preview: str
    # 行号保留给原有 TXT/Markdown 客户端；新格式同时给出实际可读的页码或段落定位。
    source_kind: Literal["line", "page", "paragraph", "table", "region", "mixed"] = "line"
    source_locator: str = ""


class WorkspaceDocumentSearchResponse(BaseModel):
    query: str
    total: int
    searched_documents: int
    limit: int
    limit_reached: bool = False
    matched_documents: list[str] = Field(default_factory=list)
    suggested_read_path: str | None = None
    matches: list[WorkspaceDocumentSearchMatch] = Field(default_factory=list)
