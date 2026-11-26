from __future__ import annotations

import json
import subprocess
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
            }, {
                "answerId": "legacy-question-7",
                "decision": "omit",
                "answerText": "此处暂不采用",
                "values": {},
                "applicability": "",
                "sourceLocation": "第 10 页",
            }],
            }, ensure_ascii=False), encoding="utf-8")

            rows = read_feedback_json(path)

        row = rows[0]
        self.assertEqual(row[ANSWER_COLUMN], "不用")
        self.assertTrue(row["_binary_permission"])
        self.assertFalse(row["_force_note"])
        self.assertEqual(rows[1]["编号"], "legacy-question-7")
        self.assertTrue(rows[1]["_from_json"])

    def test_main_applies_deny_and_reports_unmatched_legacy_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            build = run / "04_知识库构建"
            build.mkdir(parents=True)
            config = Path(temp) / "kb-config.json"
            config.write_text(json.dumps({
                "project_id": "P-999",
                "client_answers": {"doc2": "keep"},
                "client_answer_confirmations": {
                    "doc2": {"decision": "keep", "cus_id": "P-999-CUS-003.1"},
                },
            }), encoding="utf-8")
            (build / "governance.json").write_text(json.dumps({"CUS": [{
                "id": "P-999-CUS-001",
                "items": [{
                    "sub_id": "P-999-CUS-001.1",
                    "client_id": "question-1",
                    "item_id": "doc1",
                    "answer_type": "text",
                    "question": "May this complete file be included?",
                }],
            }]}), encoding="utf-8")
            feedback = Path(temp) / "feedback.json"
            feedback.write_text(json.dumps({
                "schema": "customer-feedback.v1",
                "answers": [{
                    "answerId": "question-1",
                    "decision": "deny",
                    "values": {},
                    "applicability": "",
                    "sourceLocation": "整份文件",
                }, {
                    "cusId": "P-999-CUS-006",
                    "answerId": "legacy-question-7",
                    "decision": "omit",
                    "answerText": "此处暂不采用",
                    "question": "A legacy page question",
                    "sourceName": "catalog.pdf",
                    "sourceLocation": "第 10 页",
                }, {
                    "cusId": "P-999-CUS-003",
                    "answerId": "old-approved-question",
                    "decision": "allow",
                    "answerText": "可以公开使用",
                    "values": {},
                    "applicability": "",
                    "sourceLocation": "整份文件",
                }],
            }, ensure_ascii=False), encoding="utf-8")

            result = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "apply_answers.py"),
                "--out", str(run), "--config", str(config), "--answers", str(feedback),
            ], capture_output=True, text=True, encoding="utf-8", check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            updated_config = json.loads(config.read_text(encoding="utf-8"))
            notes = [json.loads(line) for line in (build / "client-notes.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(output["applied"], 1)
        self.assertEqual(updated_config["client_answers"]["doc1"], "drop")
        self.assertEqual(output["unclear"][0]["id"], "legacy-question-7")
        self.assertIn("未应用", output["unclear"][0]["problem"])
        self.assertEqual(notes[0]["id"], "P-999-CUS-006-legacy-question-7")
        self.assertIn("catalog.pdf", notes[0]["note"])


if __name__ == "__main__":
    unittest.main()
