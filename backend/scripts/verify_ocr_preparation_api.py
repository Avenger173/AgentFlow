"""K7.4 OCR 准备 API 的离线回归。

脚本用内存假的 capability / prepare 函数验证显式确认、后台阶段、重复点击去重与失败说明。
它不会安装 Paddle、下载模型、读取客户文件或访问网络；真实模型准备仍只能由客户在 Qt 界面确认。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event
from time import monotonic, sleep


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_ocr_prepare_api_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "ocr_prepare_api_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.api import knowledge as knowledge_api
from app.services import ocr_adapter
from app.services.ocr_adapter import (
    OCR_MODEL_PROFILE,
    OcrAdapterError,
    OcrCapability,
    OcrDependencyInstallError,
)
from main import app


def _reset_ocr_preparation_state() -> None:
    """隔离本脚本的模块级后台状态，避免两条场景彼此误判为重复请求。"""

    with knowledge_api._OCR_PREPARATIONS_LOCK:
        knowledge_api._OCR_PREPARATIONS.clear()


def _wait_for_terminal(client: TestClient, preparation_id: str) -> dict[str, object]:
    """只轮询有限时间，验证真实阶段收束而不是接受静态“已提交”文案。"""

    deadline = monotonic() + 3.0
    last: dict[str, object] = {}
    while monotonic() < deadline:
        response = client.get(f"/api/knowledge/ocr-preparations/{preparation_id}")
        response.raise_for_status()
        last = response.json()
        if last["status"] in {"ready", "failed"}:
            return last
        sleep(0.02)
    raise AssertionError(f"OCR preparation did not reach terminal state: {last}")


def _verify_confirmed_background_prepare(client: TestClient) -> None:
    """能力检测不能准备模型；确认请求必须去重并最终显示 ready。"""

    state = {"ready": False, "prepare_calls": 0, "install_calls": 0}
    release_prepare = Event()

    def fake_capability() -> OcrCapability:
        return OcrCapability(
            paddleocr_available=True,
            model_initialized=state["ready"],
            profile=OCR_MODEL_PROFILE,
            message="本地 OCR 依赖已准备，需由客户确认下载并初始化模型。"
            if not state["ready"]
            else "本地 OCR 依赖与已确认模型均已准备。",
        )

    def fake_prepare(*, allow_download: bool) -> OcrCapability:
        assert allow_download is True
        state["prepare_calls"] += 1
        assert release_prepare.wait(timeout=2.0), "test did not release fake OCR preparation"
        state["ready"] = True
        return fake_capability()

    def fake_install() -> OcrCapability:
        state["install_calls"] += 1
        return fake_capability()

    original_capability = knowledge_api.ocr_capability
    original_prepare = knowledge_api.prepare_local_ocr_model
    original_install = knowledge_api.install_local_ocr_dependencies
    knowledge_api.ocr_capability = fake_capability
    knowledge_api.prepare_local_ocr_model = fake_prepare
    knowledge_api.install_local_ocr_dependencies = fake_install
    try:
        capability = client.get("/api/knowledge/ocr-capability")
        capability.raise_for_status()
        assert capability.json()["model_initialized"] is False
        assert state["prepare_calls"] == 0, "capability endpoint must never prepare/download models"

        assert client.post("/api/knowledge/ocr-model/prepare", json={}).status_code == 422
        first = client.post("/api/knowledge/ocr-model/prepare", json={"confirm_download": True})
        assert first.status_code == 202
        first_payload = first.json()
        second = client.post("/api/knowledge/ocr-model/prepare", json={"confirm_download": True})
        assert second.status_code == 202
        assert second.json()["preparation_id"] == first_payload["preparation_id"]

        release_prepare.set()
        final = _wait_for_terminal(client, str(first_payload["preparation_id"]))
        assert final["status"] == "ready"
        assert final["completed_at"]
        assert state["prepare_calls"] == 1, "duplicate confirmation must not start a second download"
        assert state["install_calls"] == 1, "confirmed preparation must check optional components once"
    finally:
        release_prepare.set()
        knowledge_api.ocr_capability = original_capability
        knowledge_api.prepare_local_ocr_model = original_prepare
        knowledge_api.install_local_ocr_dependencies = original_install


def _verify_fixed_optional_dependency_install_contract() -> None:
    """安装器只能运行当前解释器的固定 requirements 与 pip check，不能执行客户命令。"""

    state = {"available": False, "commands": []}

    def fake_capability() -> OcrCapability:
        return OcrCapability(
            paddleocr_available=state["available"],
            model_initialized=False,
            profile=OCR_MODEL_PROFILE,
            message="test capability",
        )

    def fake_run(command: list[str], **kwargs: object):
        state["commands"].append((command, kwargs))
        if "install" in command:
            state["available"] = True

        class _Result:
            returncode = 0

        return _Result()

    original_capability = ocr_adapter.ocr_capability
    original_run = ocr_adapter.subprocess.run
    try:
        ocr_adapter.ocr_capability = fake_capability
        ocr_adapter.subprocess.run = fake_run
        capability = ocr_adapter.install_local_ocr_dependencies()
        assert capability.paddleocr_available is True
        assert len(state["commands"]) == 2
        install_command, install_options = state["commands"][0]
        check_command, check_options = state["commands"][1]
        assert install_command[:7] == [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
        assert install_command[-2:] == ["-r", str(BACKEND_ROOT / "requirements-ocr.txt")]
        assert check_command == [sys.executable, "-X", "utf8", "-m", "pip", "check"]
        assert "shell" not in install_options
        assert install_options["stdout"] is ocr_adapter.subprocess.DEVNULL
        assert install_options["stderr"] is ocr_adapter.subprocess.DEVNULL
        assert check_options["timeout"] == install_options["timeout"]
    finally:
        ocr_adapter.ocr_capability = original_capability
        ocr_adapter.subprocess.run = original_run


def _verify_optional_dependency_install_failure(client: TestClient) -> None:
    """组件安装失败要停在准备前，并显示可行动、不含路径的短说明。"""

    def unavailable_capability() -> OcrCapability:
        return OcrCapability(
            paddleocr_available=False,
            model_initialized=False,
            profile=OCR_MODEL_PROFILE,
            message="本地 OCR 可选依赖未安装。",
        )

    state = {"prepare_calls": 0}

    def failed_install() -> OcrCapability:
        raise OcrDependencyInstallError("本地 OCR 可选组件安装未完成，请检查网络或磁盘空间后重试。")

    def unexpected_prepare(*, allow_download: bool) -> OcrCapability:
        state["prepare_calls"] += 1
        raise AssertionError("dependency failure must not continue to model preparation")

    original_capability = knowledge_api.ocr_capability
    original_prepare = knowledge_api.prepare_local_ocr_model
    original_install = knowledge_api.install_local_ocr_dependencies
    knowledge_api.ocr_capability = unavailable_capability
    knowledge_api.prepare_local_ocr_model = unexpected_prepare
    knowledge_api.install_local_ocr_dependencies = failed_install
    try:
        started = client.post("/api/knowledge/ocr-model/prepare", json={"confirm_download": True})
        assert started.status_code == 202
        final = _wait_for_terminal(client, started.json()["preparation_id"])
        assert final["status"] == "failed"
        assert "安装未完成" in final["message"]
        assert "\\" not in final["message"] and ":" not in final["message"]
        assert state["prepare_calls"] == 0
    finally:
        knowledge_api.ocr_capability = original_capability
        knowledge_api.prepare_local_ocr_model = original_prepare
        knowledge_api.install_local_ocr_dependencies = original_install


def main() -> None:
    try:
        _verify_fixed_optional_dependency_install_contract()
        with TestClient(app) as client:
            _reset_ocr_preparation_state()
            _verify_confirmed_background_prepare(client)
            _reset_ocr_preparation_state()
            _verify_optional_dependency_install_failure(client)
        print("OCR preparation API verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
