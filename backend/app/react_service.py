from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from hello_agents.agents.react_agent import ReActAgent
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.tools.registry import ToolRegistry

from backend.app.diagnosis import diagnose_campaign
from backend.app.metrics import get_campaign_metrics
from backend.app.retrieval import search_knowledge


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

REACT_PROMPT = """你是广告投放分析助手。你必须只依据已有确定性结果和只读工具结果回答，不能自行计算指标、创造阈值、平台规则或已确认事实。

可用工具：
{tools}

每一步只能输出两行，且不要使用 Markdown 标记：
Thought: 简短说明还缺什么信息
Action: 工具名[JSON参数]

信息充分后输出：
Thought: 已获得足够信息
Action: Finish[面向运营人员的最终答案]

规则：
1. 最终回答前至少调用一次最相关工具；信息不足时可以继续调用工具。
2. 指标和异常结论必须原样依据工具结果。
3. 区分已确认事实、可能原因和建议，不能把可能原因说成事实。
4. 只能引用工具实际返回的知识来源。
5. 不执行任何广告修改。

当前任务：
{question}

本次 ReAct 执行历史：
{history}
"""


def _parameters(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是 JSON 对象。")
    return value


def compose_with_react(
    connection: sqlite3.Connection,
    question: str,
    baseline: dict[str, object],
    conversation_history: list[dict[str, str]],
    llm: HelloAgentsLLM | None = None,
) -> dict[str, object]:
    calls: list[dict[str, object]] = []
    registry = ToolRegistry()

    def query_metrics(raw: str) -> str:
        params = _parameters(raw)
        result = get_campaign_metrics(
            connection,
            str(params["campaign_code"]),
            str(params["start_date"]),
            str(params["end_date"]),
        )
        calls.append(
            {
                "tool": "query_campaign_metrics",
                "parameters": params,
                "summary": {"calculated": result["calculated"]},
            }
        )
        return json.dumps(result, ensure_ascii=False)

    def diagnose(raw: str) -> str:
        params = _parameters(raw)
        result = diagnose_campaign(
            connection,
            str(params["campaign_code"]),
            str(params["start_date"]),
            str(params["end_date"]),
        )
        calls.append(
            {
                "tool": "diagnose_campaign",
                "parameters": params,
                "summary": {
                    "anomaly_rule_ids": [
                        item["rule_id"] for item in result["diagnosis"]["anomalies"]
                    ],
                    "sources": result["sources"],
                },
            }
        )
        return json.dumps(result, ensure_ascii=False)

    def search(raw: str) -> str:
        try:
            params = _parameters(raw)
            query = str(params["query"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            query = raw.strip()
            params = {"query": query}
        chunks = search_knowledge(connection, query)
        calls.append(
            {
                "tool": "search_knowledge",
                "parameters": params,
                "summary": {
                    "sources": [
                        {
                            "source_code": item["source_code"],
                            "title": item["title"],
                            "section_title": item["section_title"],
                        }
                        for item in chunks
                    ]
                },
            }
        )
        return json.dumps(chunks, ensure_ascii=False)

    with redirect_stdout(io.StringIO()):
        registry.register_function(
            "query_campaign_metrics",
            '查询确定性指标。输入 JSON：{"campaign_code":"CMP001","start_date":"2026-09-01","end_date":"2026-09-08"}',
            query_metrics,
        )
        registry.register_function(
            "diagnose_campaign",
            '执行已确认的异常规则。输入 JSON：{"campaign_code":"CMP001","start_date":"2026-09-08","end_date":"2026-09-08"}',
            diagnose,
        )
        registry.register_function(
            "search_knowledge",
            '检索模拟内部 SOP。输入 JSON：{"query":"没有转化怎么排查"}',
            search,
        )

    try:
        model = llm or HelloAgentsLLM(temperature=0.2)
        agent = ReActAgent(
            name="AdOps ReAct Agent",
            llm=model,
            tool_registry=registry,
            max_steps=5,
            custom_prompt=REACT_PROMPT,
        )
        history_text = "\n".join(
            f"{item['role']}: {item['content']}" for item in conversation_history[-8:]
        ) or "无"
        task = (
            f"会话历史：\n{history_text}\n\n"
            f"用户当前问题：{question}"
        )
        # hello-agents 0.2.0 会把 Thought 打印到 stdout；产品不保存或暴露该内容。
        with redirect_stdout(io.StringIO()):
            answer = agent.run(task, temperature=0.2)
        if not calls or answer == "抱歉，我无法在限定步数内完成这个任务。":
            raise RuntimeError("ReActAgent 未在最大步数内完成有效工具调用。")
        return {"answer": answer, "degraded": False, "tool_calls": calls}
    except Exception as exc:
        return {
            "answer": baseline["answer"],
            "degraded": True,
            "degraded_reason": "LLM_UNAVAILABLE",
            "tool_calls": calls,
            "error_type": type(exc).__name__,
        }
