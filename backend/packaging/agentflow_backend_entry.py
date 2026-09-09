"""AgentFlow 目录发行使用的 FastAPI 后端入口。"""

from __future__ import annotations

import uvicorn

from app.core.config import settings
from main import app


def main() -> None:
    """启动本机回环后端；宿主 Qt 进程负责端口与生命周期管理。"""

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
