"""AgentFlow 目录发行使用的 FastAPI 后端入口。"""

from __future__ import annotations

import uvicorn


def main() -> None:
    """启动本机回环后端；宿主 Qt 进程负责端口与生命周期管理。"""

    # 配置在 import 时会把数值环境变量转换为 int/float。目录发行不能把 Python traceback
    # 留给客户或 Qt；这里输出固定 ASCII 错误码，避免 Windows 控制台代码页破坏中文，
    # BackendManager 再将其转换为可操作的中文提示。
    try:
        from app.core.config import settings
        from main import app
    except ValueError:
        print("AGENTFLOW_STARTUP_CONFIG_INVALID", flush=True)
        raise SystemExit(2)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
