# -*- mode: python ; coding: utf-8 -*-
"""AgentFlowBackend 的 Windows 目录式发行规格。

只收集正式 Python 后端与只读 Harness profile，不收集 .env、data、output、客户插件或
Node node_modules。Node Harness 与便携 Node 由目录发行装配步骤按显式选项单独复制。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


BACKEND_ROOT = Path(SPECPATH).parent
ENTRYPOINT = BACKEND_ROOT / "packaging" / "agentflow_backend_entry.py"
RUNTIME_ROOT = BACKEND_ROOT / "runtime" / "deepseek_harness_node"

datas = [
    (str(BACKEND_ROOT / "app" / "agents" / "builtin"), "app/agents/builtin"),
    (str(RUNTIME_ROOT / "agentflow_profile"), "runtime/deepseek_harness_node/agentflow_profile"),
    (str(RUNTIME_ROOT / "package.json"), "runtime/deepseek_harness_node"),
    (str(RUNTIME_ROOT / "package-lock.json"), "runtime/deepseek_harness_node"),
]
binaries = []
hiddenimports = collect_submodules("app")

# 这些库包含动态导入、二进制扩展或运行期数据；显式收集后便于目录发行回读缺失项。
for package_name in (
    "chromadb",
    "fastembed",
    "fitz",
    "matplotlib",
    "numpy",
    "onnxruntime",
    "openpyxl",
    "pandas",
    "PIL",
    "pptx",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(BACKEND_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # OCR 依赖不属于 requirements.txt。构建机可能为了 K7 开发安装 Paddle/PaddleX，
    # 但目录发行的默认后端必须仍显示“可选组件未安装”，并在客户确认后才走安装入口。
    # 否则构建机环境会静默改变客户发行物的能力、体积与离线故障行为。
    excludes=[
        "pytest",
        "tests",
        "paddle",
        "paddleocr",
        "paddlex",
        "paddlenlp",
        "paddle2onnx",
        "paddleslim",
    ],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgentFlowBackend",
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="AgentFlowBackend",
)
