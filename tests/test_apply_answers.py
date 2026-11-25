from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from apply_answers import ANSWER_COLUMN, read_feedback_json  # noqa: E402


class FeedbackJsonTests(unittest.TestCase):
    def test_whole_file_deny_is_a_binary_permission_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "feedback.json"
            path.write_text(json.dumps({
                "schema": "customer-feedback.v1",
                "answers": [{
                    "answerId": "question-1",
                    "decision": "deny",
                    "answerText": "这份资料不纳入知识库。",
                    "values": {},
                    "applicability": "",
                    "sourceLocation": "整份文件",
                }],
            }, ensure_ascii=False), encoding="utf-8")

            row = read_feedback_json(path)[0]

        self.assertEqual(row[ANSWER_COLUMN], "不用")
        self.assertTrue(row["_binary_permission"])
        self.assertFalse(row["_force_note"])


if __name__ == "__main__":
    unittest.main()
