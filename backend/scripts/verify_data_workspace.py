"""数据工作台 D1 的离线接口回归。

验证只创建临时合成 Excel/CSV，不调用模型、网络或真实用户文件。输出仅含行列与字段类型等
聚合统计，确保回归日志不会泄露表格单元格内容。
"""

from __future__ import annotations

import atexit
import base64
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import quote

from fastapi.testclient import TestClient
from openpyxl import Workbook


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_data_workspace_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
atexit.register(lambda: shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True))
sys.path.insert(0, str(BACKEND_ROOT))

from main import app  # noqa: E402  # 环境变量必须在导入 Settings 前写入。


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sample_xlsx() -> bytes:
    workbook = Workbook()
    note_sheet = workbook.active
    note_sheet.title = "说明"
    note_sheet.append(["本工作表仅用于说明，不是主数据表。"])
    sheet = workbook.create_sheet("销售明细")
    # 第一行是标题，第二行才是真实表头，用于验证候选表头识别而不是硬编码第 1 行。
    sheet.append(["2026 年模拟销售明细"])
    sheet.append(["日期", "区域", "金额", "订单号", "备注"])
    sheet.append(["2026-01-01", "华东", 1200, "A-001", ""])
    sheet.append(["2026-01-02", "华南", 800, "A-002", "促销"])
    sheet.append(["2026-01-02", "华南", 800, "A-002", "促销"])
    sheet.append(["2026-01-03", "华北", None, "A-003", "待核对"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def main() -> None:
    xlsx_bytes = _sample_xlsx()
    source_path = VERIFY_DATA_DIR / "source.xlsx"
    source_path.write_bytes(xlsx_bytes)
    before_hash = sha256(source_path.read_bytes()).hexdigest()

    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "销售样本.xlsx", "content_base64": _base64(xlsx_bytes)},
        )
        assert imported.status_code == 200, imported.text
        imported_payload = imported.json()
        assert imported_payload["dataset_type"] == "xlsx"

        profile = client.get(
            "/api/agents/data_agent/datasets/销售样本.xlsx/profile",
        )
        assert profile.status_code == 200, profile.text
        payload = profile.json()
        assert payload["selected_sheet"] == "销售明细"
        assert payload["header_row"] == 2
        assert payload["row_count"] == 4
        assert payload["column_count"] == 5
        assert len(payload["preview_rows"]) <= 20
        assert len(payload["preview_columns"]) <= 20
        columns = {item["name"]: item for item in payload["columns"]}
        assert columns["金额"]["inferred_type"] == "number"
        assert columns["日期"]["inferred_type"] == "date"
        assert payload["quality_summary"]["duplicate_row_count"] == 1
        assert payload["quality_summary"]["missing_cell_count"] >= 2
        assert sha256(source_path.read_bytes()).hexdigest() == before_hash

        # CSV 使用 GB18030 和分号，验证中文 Windows 常见导出格式不需要用户手改编码。
        csv_bytes = "月份;渠道;订单数\n2026-01;线上;12\n2026-02;线下;8\n".encode("gb18030")
        csv_imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "渠道数据.csv", "content_base64": _base64(csv_bytes)},
        )
        assert csv_imported.status_code == 200, csv_imported.text
        csv_profile = client.get("/api/agents/data_agent/datasets/渠道数据.csv/profile")
        assert csv_profile.status_code == 200, csv_profile.text
        assert csv_profile.json()["row_count"] == 2
        assert csv_profile.json()["column_count"] == 3

        # Excel/WPS 的“Unicode CSV”常带 UTF-16 BOM；这是客户实际导入时不能要求手工转码的
        # 格式。另以竖线分隔验证有限分隔符识别不会只依赖逗号或分号。
        utf16_csv_bytes = "日期|区域|金额\r\n2026-01-01|华东|1200\r\n2026-01-02|华南|800\r\n".encode("utf-16")
        utf16_imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "Unicode 导出.csv", "content_base64": _base64(utf16_csv_bytes)},
        )
        assert utf16_imported.status_code == 200, utf16_imported.text
        utf16_profile = client.get("/api/agents/data_agent/datasets/Unicode 导出.csv/profile")
        assert utf16_profile.status_code == 200, utf16_profile.text
        assert utf16_profile.json()["row_count"] == 2
        assert utf16_profile.json()["column_count"] == 3

        # 真实 UTF-8 文件可能恰好在 64KB 编码嗅探窗口的末尾切开中文多字节字符。该用例保证
        # 解析器只回退采样末尾、完整文件仍交由 pandas 原样读取，不会把合法 CSV 误报为编码失败。
        boundary_header = b"sentence,label\n"
        boundary_padding = b"x" * (65_535 - len(boundary_header))
        boundary_csv_bytes = boundary_header + boundary_padding + "中".encode("utf-8") + b",1\nnext,2\n"
        boundary_imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "utf8_boundary.csv", "content_base64": _base64(boundary_csv_bytes)},
        )
        assert boundary_imported.status_code == 200, boundary_imported.text
        boundary_profile = client.get("/api/agents/data_agent/datasets/utf8_boundary.csv/profile")
        assert boundary_profile.status_code == 200, boundary_profile.text
        assert boundary_profile.json()["row_count"] == 2
        assert boundary_profile.json()["column_count"] == 2

        # Qt/HTTP 客户端会对路径段编码。文件名包含中文、空格和括号时，后端必须按一次
        # 正确解码后的自然文件名定位工作区副本；这能覆盖 Windows 用户常见的导出命名方式。
        encoded_filename = "渠道 数据 (最终版).csv"
        encoded_imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": encoded_filename, "content_base64": _base64(csv_bytes)},
        )
        assert encoded_imported.status_code == 200, encoded_imported.text
        encoded_profile = client.get(
            f"/api/agents/data_agent/datasets/{quote(encoded_filename, safe='')}/profile",
        )
        assert encoded_profile.status_code == 200, encoded_profile.text
        assert encoded_profile.json()["row_count"] == 2

        listed = client.get("/api/agents/data_agent/datasets")
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 5

        unsafe = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "../outside.csv", "content_base64": _base64(csv_bytes)},
        )
        assert unsafe.status_code == 400
        missing = client.get("/api/agents/data_agent/datasets/missing.csv/profile")
        assert missing.status_code == 404

    # 回归输出故意不打印 preview_rows 或合成单元格；只陈述通过的边界与结构统计。
    print("Data workspace verification passed: xlsx=4x5 csv=utf8/gb18030/utf16/boundary/path-encoding profiles=true source_unchanged=true")


if __name__ == "__main__":
    main()
