"""PPT 研究 Provider 共用的网络策略。

Windows 的 WinINET“系统代理”并不总会被 Python ``httpx`` 自动继承。这里允许用户通过
专用环境变量明确给 ResearchGateway 配置代理，同时保留 ``direct`` 这个需用户主动选择的
直连模式；业务代码不读取注册表、不猜测代理地址、更不会把代理凭据写进任务日志。
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.config import settings


def research_httpx_options() -> dict[str, object]:
    """返回给 ``httpx.Client`` 的网络选项，配置非法时保守退回环境策略。

    当用户设置了专用代理时关闭 ``trust_env``，避免环境变量中的另一套代理覆盖用户明确的
    ResearchGateway 配置。代理 URL 支持 HTTP/HTTPS；认证信息即使存在也只留在进程内。
    """

    mode = settings.presentation_research_network_mode
    if mode == "direct":
        return {"trust_env": False}

    proxy_url = settings.presentation_research_proxy_url.strip()
    if _is_valid_proxy_url(proxy_url):
        return {"trust_env": False, "proxy": proxy_url}
    return {"trust_env": True}


def _is_valid_proxy_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
