"""验证 Provider 配置、任务参数和视觉模型路由的完整闭环。

脚本只使用临时数据目录与假环境变量，不连接任何模型供应商，也不读取开发配置。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_model_configuration_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
os.environ["AGENTFLOW_LLM_PROVIDER"] = "deepseek"
os.environ["AGENTFLOW_LLM_API_KEY"] = "fixture-global-deepseek-key"
os.environ["DEEPSEEK_API_KEY"] = "fixture-deepseek-key"
os.environ["KIMI_API_KEY"] = "fixture-kimi-key"
os.environ["SEEDREAM_API_KEY"] = "fixture-seedream-key"
os.environ["AGENTFLOW_SEEDREAM_BASE_URL"] = "https://ark-env.example.test/api/v3"
os.environ["AGENTFLOW_SEEDREAM_MODEL"] = "seedream-env-fixture"
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.schemas.model import ModelGenerationParameters
from app.services.model_config_store import ModelConfigRepository
from app.services.model_gateway import (
    _apply_openai_runtime_options,
    model_provider_api_key_source,
    resolve_model_runtime_for_route,
    resolve_model_runtime_for_test,
    resolve_visual_model_runtime_for_route,
)
from main import app


def _verify_legacy_configuration_migration() -> None:
    path = VERIFY_DATA_DIR / "legacy_model_config.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "legacy-model",
                "thinking": "disabled",
                "parameters": {"temperature": 0.18, "max_tokens": 3072},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository = ModelConfigRepository(path)
    loaded = repository.load()
    migrated = loaded.provider_config_for("deepseek")
    assert migrated is not None
    assert migrated.model == "legacy-model"
    assert migrated.parameters.temperature == 0.18

    repository.save(
        provider="seedream",
        base_url="https://ark.example.test/api/v3",
        model="seedream-fixture",
        thinking="disabled",
        set_as_default=False,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["provider"] == "deepseek"
    assert payload["provider_configs"]["deepseek"]["model"] == "legacy-model"
    assert payload["provider_configs"]["seedream"]["model"] == "seedream-fixture"


def _verify_http_and_runtime_configuration() -> None:
    with TestClient(app) as client:
        providers_response = client.get("/api/models/providers")
        providers_response.raise_for_status()
        providers = {item["provider"]: item for item in providers_response.json()["providers"]}
        assert providers["seedream"]["model_kind"] == "image"
        assert providers["seedream"]["supports_visual_generation"] is True
        assert providers["seedream"]["api_key_configured"] is True
        assert providers["seedream"]["configured_base_url"] == "https://ark-env.example.test/api/v3"
        assert providers["seedream"]["configured_model"] == "seedream-env-fixture"
        assert providers["kimi"]["supports_temperature"] is False
        assert providers["anthropic"]["supports_top_p"] is False
        assert model_provider_api_key_source("qwen") == "none"

        deepseek_save = client.put(
            "/api/models/config",
            json={
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "thinking": "disabled",
                "parameters": {
                    "temperature": 0.41,
                    "top_p": 0.92,
                    "max_tokens": 4096,
                    "presence_penalty": 0.1,
                    "frequency_penalty": 0.2,
                },
            },
        )
        deepseek_save.raise_for_status()
        assert deepseek_save.json()["parameters"]["temperature"] == 0.41

        seedream_save = client.put(
            "/api/models/config",
            json={
                "provider": "seedream",
                "base_url": "https://ark.example.test/api/v3",
                "model": "seedream-fixture",
                "set_as_default": False,
            },
        )
        seedream_save.raise_for_status()
        assert seedream_save.json()["model_kind"] == "image"
        assert seedream_save.json()["api_key_source"] == "environment"

        current = client.get("/api/models/config")
        current.raise_for_status()
        assert current.json()["provider"] == "deepseek"
        assert current.json()["model"] == "deepseek-v4-flash"

        saved_config = json.loads((VERIFY_DATA_DIR / "model_config.json").read_text(encoding="utf-8"))
        assert saved_config["version"] == 3
        assert saved_config["provider"] == "deepseek"
        assert saved_config["provider_configs"]["seedream"]["model"] == "seedream-fixture"
        assert "fixture-deepseek-key" not in json.dumps(saved_config)
        assert "fixture-seedream-key" not in json.dumps(saved_config)

        routes_response = client.get("/api/models/routes")
        routes_response.raise_for_status()
        routes = {item["route_id"]: item for item in routes_response.json()["routes"]}
        assert routes["commander_planning"]["resolved"]["parameters"]["temperature"] == 0.3
        assert routes["document_analysis"]["resolved"]["parameters"]["temperature"] == 0.2
        assert routes["document_presentation"]["resolved"]["parameters"]["temperature"] == 0.7
        assert routes["visual_generation"]["availability"] == "ready"
        assert routes["visual_generation"]["resolved"]["model"] == "seedream-fixture"

        route_save = client.put(
            "/api/models/routes/document_presentation",
            json={
                "mode": "inherit_global",
                "parameters": {"temperature": 0.83, "top_p": 0.88, "max_tokens": 6144},
            },
        )
        route_save.raise_for_status()
        resolved_parameters = route_save.json()["resolved"]["parameters"]
        assert resolved_parameters["temperature"] == 0.83
        assert resolved_parameters["top_p"] == 0.88
        assert resolved_parameters["max_tokens"] == 6144

        runtime = resolve_model_runtime_for_route("document_presentation").runtime
        request_payload: dict[str, object] = {}
        _apply_openai_runtime_options(request_payload, runtime)
        assert request_payload["temperature"] == 0.83
        assert request_payload["top_p"] == 0.88
        assert runtime.max_tokens == 6144

        visual_runtime = resolve_visual_model_runtime_for_route().runtime
        assert visual_runtime.provider == "seedream"
        assert visual_runtime.base_url == "https://ark.example.test/api/v3"
        assert visual_runtime.model == "seedream-fixture"
        assert visual_runtime.api_key_configured

        rejected = client.put(
            "/api/models/config",
            json={
                "provider": "kimi",
                "base_url": "https://api.moonshot.cn/v1",
                "model": "kimi-k2.6",
                "parameters": {"temperature": 0.5},
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "温度" in rejected.json()["detail"]

        catalog = client.post("/api/models/catalog", json={"provider": "custom"})
        catalog.raise_for_status()
        assert catalog.json()["source"] == "recommended"

    kimi_runtime, _ = resolve_model_runtime_for_test(provider="kimi")
    kimi_payload: dict[str, object] = {}
    _apply_openai_runtime_options(kimi_payload, kimi_runtime)
    assert "temperature" not in kimi_payload
    assert "top_p" not in kimi_payload


def main() -> None:
    _verify_legacy_configuration_migration()
    _verify_http_and_runtime_configuration()
    print("Model configuration verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
