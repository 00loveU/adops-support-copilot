from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.database import connect, initialize_database
from backend.app.react_service import compose_with_react


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def invoke(self, _messages, **_kwargs) -> str:
        return next(self.responses)


class FailingLLM:
    def invoke(self, _messages, **_kwargs) -> str:
        raise RuntimeError("simulated outage")


class ReactServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "react-test.db"
        initialize_database(self.db_path)
        self.connection = connect(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_agent_can_observe_a_tool_then_finish(self) -> None:
        llm = FakeLLM(
            [
                'Thought: 需要查询内部口径\nAction: search_knowledge[{"query":"CTR 是什么意思"}]',
                "Thought: 已获得足够信息\nAction: Finish[CTR 等于点击量除以曝光量。]",
            ]
        )
        result = compose_with_react(
            self.connection,
            "CTR 是什么意思？",
            {"answer": "fallback", "sources": [{"source_code": "RULE-METRIC-001"}]},
            [{"role": "user", "content": "请使用内部口径"}],
            llm=llm,
        )
        self.assertFalse(result["degraded"])
        self.assertEqual("CTR 等于点击量除以曝光量。", result["answer"])
        self.assertEqual("search_knowledge", result["tool_calls"][0]["tool"])

    def test_agent_failure_returns_deterministic_answer(self) -> None:
        result = compose_with_react(
            self.connection,
            "CTR 是什么意思？",
            {"answer": "确定性兜底", "sources": []},
            [],
            llm=FailingLLM(),
        )
        self.assertTrue(result["degraded"])
        self.assertEqual("LLM_UNAVAILABLE", result["degraded_reason"])
        self.assertEqual("确定性兜底", result["answer"])


if __name__ == "__main__":
    unittest.main()
