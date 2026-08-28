"""知识库 K0 的确定性夹具与 SQLite FTS5 基线验证。

该脚本故意不导入 AgentFlow 的 API、模型网关或真实用户资料。它只验证第一层候选：
Windows 自带 SQLite 的 FTS5 是否可用，以及中文关键词影子字段能否可靠补足 unicode61
对连续中文文本的分词不足。向量检索、Embedding 与 Rerank 将在独立候选试验中接入同一份题集。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures" / "knowledge_k0"
QUESTIONS_PATH = FIXTURE_DIR / "question_set.json"
CHINESE_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
ASCII_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _document_id(path: Path) -> str:
    """夹具 ID 由稳定文件名导出，后续向量候选也必须沿用这一来源身份。"""

    return path.stem


def _chinese_shadow_terms(text: str) -> list[str]:
    """生成仅用于 FTS 匹配的中文单字与相邻二元词，不改变展示或引用的原始文本。"""

    terms: list[str] = []
    for sequence in CHINESE_SEQUENCE.findall(text):
        terms.extend(sequence)
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms


def _fts_query(question: str) -> str:
    """把问题转换为受控 MATCH 词项，避免把客户输入直接拼进 FTS 查询语法。"""

    ascii_terms = [match.group(0).lower() for match in ASCII_TERM.finditer(question)]
    # 标识符是精确检索的强信号。AF-204 之类的编号不能与问题中的自然语言虚词混合 AND。
    if ascii_terms:
        terms = ascii_terms
    else:
        # 中文问题不能把每个相邻词都强制 AND：例如“审批窗口是什么时候”中的疑问部分
        # 通常不在原文中。二元词 OR 召回后再交给 BM25 排序，先得到稳定的关键词候选。
        terms = [term for term in _chinese_shadow_terms(question) if len(term) == 2]
    # 每一个词项都加双引号，避免客户输入被解释成 FTS5 查询语法；OR 也由本函数固定生成。
    quoted_terms = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in dict.fromkeys(terms)]
    return " OR ".join(quoted_terms)


def _load_fixture_documents() -> list[tuple[str, str, str]]:
    """读取项目内自带、脱敏且固定的 Markdown 夹具。"""

    documents: list[tuple[str, str, str]] = []
    for path in sorted(FIXTURE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        documents.append((_document_id(path), path.stem.replace("_", " "), text))
    if not documents:
        raise RuntimeError("知识库 K0 夹具为空。")
    return documents


def _build_fts5_index(connection: sqlite3.Connection, documents: list[tuple[str, str, str]]) -> None:
    """建立临时 FTS5 索引；真实产品索引会由 K1 的可版本化服务持久化。"""

    connection.execute(
        "CREATE VIRTUAL TABLE knowledge_k0_fts USING fts5("
        "document_id UNINDEXED, title, body, cjk_shadow, tokenize='unicode61 remove_diacritics 2'"
        ")"
    )
    for document_id, title, body in documents:
        connection.execute(
            "INSERT INTO knowledge_k0_fts(document_id, title, body, cjk_shadow) VALUES (?, ?, ?, ?)",
            (document_id, title, body, " ".join(_chinese_shadow_terms(body))),
        )


def _search(connection: sqlite3.Connection, question: str) -> list[str]:
    """按 BM25 返回候选文档 ID；K0 只关心召回，不把分数伪装成跨检索器通用分数。"""

    match_query = _fts_query(question)
    if not match_query:
        return []
    rows = connection.execute(
        "SELECT document_id FROM knowledge_k0_fts "
        "WHERE knowledge_k0_fts MATCH ? "
        "ORDER BY bm25(knowledge_k0_fts, 0.0, 1.0, 0.8, 1.8) LIMIT 5",
        (match_query,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def main() -> None:
    documents = _load_fixture_documents()
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="agentflow_knowledge_k0_") as temporary_dir:
        database_path = Path(temporary_dir) / "fts5_probe.db"
        connection = sqlite3.connect(database_path)
        try:
            _build_fts5_index(connection, documents)
            fts5_enabled = connection.execute("SELECT fts5_source_id()").fetchone() is not None
            assert fts5_enabled, "当前 Python SQLite 未启用 FTS5。"

            required_cases = [item for item in questions if item["baseline_required"]]
            passed_cases = 0
            for item in required_cases:
                results = _search(connection, str(item["question"]))
                expected = set(item["expected_document_ids"])
                assert expected.intersection(results), (
                    f"K0 FTS5 基线未召回 {item['id']} 的预期夹具；"
                    f"expected={sorted(expected)} actual={results}"
                )
                passed_cases += 1
        finally:
            # sqlite3.Connection 的上下文管理器只负责事务，不会自动 close；Windows 下必须
            # 在 TemporaryDirectory 清理前显式关闭，避免留下被占用的索引文件。
            connection.close()

    # 输出只陈述夹具规模和确定性指标，不打印资料正文或检索片段。
    print(
        "Knowledge K0 FTS5 baseline passed: "
        f"documents={len(documents)} questions={len(questions)} "
        f"required_recall_at_5={passed_cases}/{len(required_cases)} "
        "fts5=true cjk_shadow=true network=false model_calls=0"
    )


if __name__ == "__main__":
    main()
