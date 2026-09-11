"""长期记忆检索共享的确定性文本归一化。

该模块不访问数据库、模型或配置。SQLite migration 与 Repository 都依赖同一份中文二元词
规则，避免新写入记录和查询端因分词差异出现“明明保存了却检索不到”的假漏召回。
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_.:+-]{1,79}", re.IGNORECASE)
_CHINESE_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def build_memory_search_terms(value: str, *, maximum: int = 32) -> list[str]:
    """为短事实生成有限 ASCII 标识符与中文二元词。

    中文没有通用的空格分词；二元词可让“交付格式”“格式要求”之类的短表达获得稳定的
    FTS 召回，同时不引入分词器、网络或第二套索引依赖。
    """

    normalized = " ".join(value.lower().split())
    if not normalized or maximum < 1:
        return []
    terms: list[str] = []
    terms.extend(match.group(0).lower() for match in _ASCII_TERM.finditer(normalized))
    for sequence in _CHINESE_SEQUENCE.findall(normalized):
        if len(sequence) == 1:
            terms.append(sequence)
            continue
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return list(dict.fromkeys(term for term in terms if term))[:maximum]


def build_memory_fts_shadow(values: Iterable[str]) -> str:
    """建立不含额外正文的 FTS 词项影子字段。"""

    terms: list[str] = []
    for value in values:
        terms.extend(build_memory_search_terms(str(value), maximum=64))
    return " ".join(dict.fromkeys(terms))


def build_memory_fts_match(value: str) -> str:
    """由已清洗词项构造 FTS5 OR 查询，禁止客户文本注入 FTS 运算符。"""

    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in build_memory_search_terms(value)
    )
