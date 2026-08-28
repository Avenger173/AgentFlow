from pydantic import BaseModel, Field


class HealthCapability(BaseModel):
    """单项本地能力的轻量就绪状态，不包含客户数据或环境绝对路径。"""

    ready: bool
    message: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    capabilities: dict[str, HealthCapability] = Field(default_factory=dict)
