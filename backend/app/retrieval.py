from __future__ import annotations

import sqlite3


NO_RESULT_ANSWER = (
    "当前模拟知识库没有该问题的可靠依据。"
    "本系统仅支持投放指标、无曝光、CTR、CVR、CPA、ROAS 和消耗波动相关问题。"
)


def normalize_query(message: str) -> str:
    """把已确认的用户表达映射为知识库中的规范检索短语。"""
    text = message.strip()
    lowered = text.casefold()

    if any(word in text for word in ("没有曝光", "无曝光", "没有展现", "零曝光", "投放不出去")):
        return "没有曝光"
    if any(word in text for word in ("没有转化", "无转化", "没有成交", "无成交")):
        return "没有转化"
    if any(word in text for word in ("点击很少", "点击少", "曝光多点击少")):
        return "点击很少"
    if any(word in lowered for word in ("转化成本", "获客成本", "cpa")) and any(
        word in text for word in ("高", "贵", "上涨", "上升")
    ):
        return "转化成本"
    if any(word in lowered for word in ("广告投产比", "投产比", "roas", "roi")) and any(
        word in text for word in ("低", "差", "亏", "下降")
    ):
        return "广告投产比"
    if any(word in text for word in ("消耗突然上涨", "消耗上涨", "消耗上升", "消耗突增", "花费突增")):
        return "消耗突然上涨"
    if any(word in text for word in ("消耗下降", "消耗波动", "花费骤降")):
        return "消耗波动"
    if any(word in lowered for word in ("ctr", "cvr", "cpc", "cpa", "roas", "roi")) and any(
        word in text for word in ("是什么", "什么意思", "怎么计算", "如何计算", "口径")
    ):
        return "指标口径"
    return text


def search_knowledge(
    connection: sqlite3.Connection, message: str, limit: int = 3
) -> list[dict[str, object]]:
    query = normalize_query(message)
    if len(query) < 3:
        return []
    escaped = query.replace('"', '""')
    rows = connection.execute(
        "SELECT chunk_id, source_code, title, section_title, content, bm25(knowledge_fts) AS rank "
        "FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
        (f'"{escaped}"', limit),
    ).fetchall()
    return [dict(row) for row in rows]


def answer_rule_question(
    connection: sqlite3.Connection, message: str
) -> dict[str, object]:
    chunks = search_knowledge(connection, message)
    if not chunks:
        return {"answer": NO_RESULT_ANSWER, "sources": [], "retrieved_chunks": []}

    answer_parts = [
        f"根据模拟内部知识库《{chunk['title']}》的“{chunk['section_title']}”：{chunk['content']}"
        for chunk in chunks
    ]
    sources: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        key = (str(chunk["source_code"]), str(chunk["section_title"]))
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "source_code": chunk["source_code"],
                "title": chunk["title"],
                "section_title": chunk["section_title"],
            }
        )
    return {
        "answer": "\n\n".join(answer_parts),
        "sources": sources,
        "retrieved_chunks": chunks,
    }
