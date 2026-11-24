#!/usr/bin/env python3
"""Decisions laid over the ledgers: few things to decide, nothing asked twice, nothing growing on reruns."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import governance  # noqa: E402
import governance_state as gs  # noqa: E402
from kb_common import write_json  # noqa: E402


def record(rid: str, key: str, title: str, details: list[str], items: list | None = None) -> dict:
    return {"id": rid, "key": key, "row": [title, f"{len(details)} 页", "待核对", "知识库专员"], "details": details,
            "items": items or [], "updated": "2026-11-24"}


def findings() -> dict:
    pages = [f"- [[画册#第 {i} 页|画册 · 第 {i} 页]]：表头错位" for i in range(1, 61)]
    return {
        "ANM": [record("P-999-ANM-002", "ai_read", "22 页有 AI 识别的文字", [f"- [[画册#第 {i} 页|第 {i} 页]]" for i in range(22)]),
                record("P-999-ANM-004", "text_errors", "60 页提取的文字和原图不一致", ["- 为什么有风险：……", "", *pages])],
        "MAT": [record("P-999-MAT-007", "dimension_drawing_model", "尺寸图型号待确认", [f"- 图 {i}：型号待确认" for i in range(55)])],
        "CUS": [record("P-999-CUS-006", "pairing", "逐项确认型号", ["- 说明"],
                       [{"key": f"k{i}", "item_id": "", "location": f"第 {i} 页", "question": f"第 {i} 页款号？", "sub_id": f"P-999-CUS-006.{i}"}
                        for i in range(1, 4)]),
                record("P-999-CUS-007", "pending:doc1", "《画册》能否进知识库", ["- 为什么问：……"],
                       [{"key": "doc1", "item_id": "doc1", "location": "", "question": "能否公开使用？", "sub_id": "P-999-CUS-007.1"}])],
    }


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.build = Path(self.temp.name)
        write_json(self.build / "kb-config.json", {"project_id": "P-999"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_overlay(self) -> tuple[dict, dict]:
        records = findings()
        summary = gs.overlay(self.build, records)
        write_json(self.build / "governance.json", {k: [{x: y for x, y in r.items() if x != "details"} for r in v]
                                                     for k, v in records.items()})
        return records, summary

    def test_marks_are_not_tasks_and_evidence_is_counted_apart(self) -> None:
        records, summary = self.run_overlay()
        self.assertEqual(records["ANM"][0]["row"][2], "风险保留")  # AI-read pages: a mark, not a to-do.
        self.assertEqual(summary["internal_groups"], 2)            # 60 pages and 55 drawings: two decisions.
        self.assertEqual(summary["risk_retained"], 1)
        self.assertEqual(summary["evidence"], 22 + 60 + 55 + 3 + 1)

    def test_evidence_closes_one_by_one_and_reruns_do_not_grow(self) -> None:
        self.run_overlay()
        result = gs.collect(self.build, [{"id": "P-999-ANM-004", "action": "close", "members": [f"第 {i} 页|" for i in range(1, 59)],
                                          "note": "已对照原图修正"}])
        self.assertEqual(result["problems"], [])
        records, summary = self.run_overlay()
        anm = next(r for r in records["ANM"] if r["id"] == "P-999-ANM-004")
        self.assertIn("剩 2 / 60", anm["row"][2])
        state = gs.load_state(self.build)
        before = copy.deepcopy(state["items"])
        self.run_overlay()
        self.assertEqual(gs.load_state(self.build)["items"], before)  # Same input twice: nothing grows.

    def test_drawings_are_one_decision_and_new_evidence_reopens(self) -> None:
        self.run_overlay()
        gs.collect(self.build, [{"id": "P-999-MAT-007", "action": "close", "note": "整组保留为型号待确认"}])
        records, summary = self.run_overlay()
        self.assertEqual(summary["internal_groups"], 1)
        more = findings()
        more["MAT"][0]["details"].append("- 图 99：型号待确认")
        gs.overlay(self.build, more)
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-MAT-007"]["status"], "action_needed")

    def test_a_batch_is_exported_once_then_sent_then_answered_field_by_field(self) -> None:
        self.run_overlay()
        exported = gs.cus_export(self.build, None)
        self.assertEqual(exported["exported"], 2)
        sheet = (self.build / f"客户确认表-{exported['batch']}.csv").read_text(encoding="utf-8-sig")
        self.assertLess(sheet.index("一、资料公开与授权"), sheet.index("二、事实与参数"))
        self.assertEqual(gs.cus_export(self.build, None)["exported"], 0)  # Not asked twice.
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-CUS-006"]["status"], "exported")
        gs.cus_sent(self.build, exported["batch"])
        gs.mark_answered(self.build, ["P-999-CUS-006.1", "P-999-CUS-006.2"])
        records, _ = self.run_overlay()
        cus = next(r for r in records["CUS"] if r["id"] == "P-999-CUS-006")
        self.assertIn("已发待答", cus["row"][2])
        self.assertIn("剩 1 / 3", cus["row"][2])
        gs.mark_answered(self.build, ["P-999-CUS-006.3"])
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-CUS-006"]["status"], "answered")

    def test_a_flagged_value_becomes_one_question_and_one_guard(self) -> None:
        self.run_overlay()
        result = gs.flag_value(self.build, "产品系列-YFD71F", "参数", "292", "292 是否应为 29.2？", row="DN65", column="PN10")
        self.assertEqual(result["guards"], 1)
        again = gs.flag_value(self.build, "产品系列-YFD71F", "参数", "292", "292 是否应为 29.2？", row="DN65", column="PN10")
        self.assertEqual(again["cus"], result["cus"])
        records = json.loads((self.build / "governance.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(r["id"] == result["cus"] for r in records["CUS"]), 1)
        gs.collect(self.build, [{"id": result["cus"], "action": "close", "note": "客户确认为 29.2，已更新"}])
        self.assertEqual(json.loads((self.build / gs.GUARDS).read_text(encoding="utf-8"))["guards"], [])

    def test_escalating_to_the_client_needs_a_question(self) -> None:
        self.run_overlay()
        bad = gs.collect(self.build, [{"id": "P-999-ANM-004", "action": "to_cus"}])
        self.assertTrue(bad["problems"])
        good = gs.collect(self.build, [{"id": "P-999-ANM-004", "action": "to_cus", "question": "第 3 页原图看不清，能否给清晰原稿？"}])
        self.assertEqual(good["problems"], [])
        records = json.loads((self.build / "governance.json").read_text(encoding="utf-8"))
        self.assertTrue(any(r["key"].startswith("escalated:") for r in records["CUS"]))
        anm = next(r for r in records["ANM"] if r["id"] == "P-999-ANM-004")
        self.assertIn("已转 CUS", anm["row"][2])

    def test_review_lists_only_decisions_and_cited_pages_first(self) -> None:
        self.run_overlay()
        text = gs.review(self.build).read_text(encoding="utf-8")
        self.assertIn("需要做决定 4 组", text)  # 2 internal + 2 questions not yet sent.
        self.assertIn("A1", text)
        self.assertNotIn("P-999-ANM-002｜", text)  # Risk kept, not listed as a decision.
        template = json.loads((self.build / "governance-decisions-template.json").read_text(encoding="utf-8"))
        self.assertEqual({d["ref"][0] for d in template["decisions"]}, {"A", "B"})

    def test_stages_people_wrote_are_read_and_win(self) -> None:
        self.run_overlay()
        vault = self.build / "vault"
        ledger = (vault / governance.LEDGERS["ANM"][0])
        ledger.parent.mkdir(parents=True)
        ledger.write_text("| ANM ID | 发生了什么 | 涉及 | 当前阶段 | 下一责任人 | 最近更新 |\n| --- | --- | --- | --- | --- | --- |\n"
                          "| P-999-ANM-004 | x | 60 页 | 已核实，可结案 | — | 2026-11-23 |\n", encoding="utf-8")
        self.assertEqual(gs.sync(self.build, vault)["taken"], 1)
        records, _ = self.run_overlay()
        self.assertEqual(next(r for r in records["ANM"] if r["id"] == "P-999-ANM-004")["row"][2], "已关闭")
        # What a person typed keeps showing at publish; a stage the state wrote is replaced by the new one.
        typed = ledger.read_text(encoding="utf-8")
        self.assertIn("已核实，可结案", governance.merge_ledger(typed, typed.replace("已核实，可结案", "已关闭")))
        rendered = typed.replace("已核实，可结案", "待内部处理")
        self.assertIn("| 已关闭 |", governance.merge_ledger(rendered, typed.replace("已核实，可结案", "已关闭")))

    def test_a_guarded_value_is_masked_in_full_tables_and_flagged_in_the_article(self) -> None:
        import check_article
        import kb_lookup
        vault = self.build / "vault-root"
        page = vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-YFD71F.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\ntitle: \"产品系列-YFD71F\"\n---\n\n# 产品系列-YFD71F\n\n## 参数\n\n### 扭矩\n\n"
                        "| DN | PN10 DRY |\n| --- | --- |\n| DN65 | 292 |\n| DN80 | 35 |\n", encoding="utf-8")
        write_json(vault / kb_lookup.GUARDS, {"guards": [{"id": "P-999-CUS-024", "page": "产品系列-YFD71F", "section": "参数",
                                                          "row": "DN65", "column": "PN10 DRY", "value": "292", "question": "292？"}]})
        result = kb_lookup.pack(vault, ["产品系列-YFD71F#参数"], [], None, False, True, brief=False)
        self.assertNotIn("| DN65 | 292 |", result["text"])
        self.assertIn("[待客户核实，不可引用：P-999-CUS-024]", result["text"])
        self.assertIn("原印 292，待客户核实", result["text"])  # Listed in 口径避坑 too.
        self.assertTrue(result["disputed_values"][0]["located"])
        checked = check_article.check("The PN10 dry torque at DN65 is 292 Nm; at DN80 it is 35 Nm.", result["text"], [], [], None,
                                      None, result["disputed_values"])
        self.assertTrue(any("P-999-CUS-024" in r["text"] for r in checked["risky"]))
        self.assertFalse(any("292" in m["text"] for m in checked["missing"]))

    def test_literal_newlines_in_details_become_lines(self) -> None:
        text = governance.render_ledger("CUS", "t", "p", "P-999", "2026-11-24",
                                        [record("P-999-CUS-024", "review:x", "292？", ["- a\\n\\n### P-999-CUS-025｜假标题"])])
        self.assertNotIn("\\n", text)

    def test_plain_words_are_allowed_but_certificates_and_brands_stay(self) -> None:
        (self.build / "review-queue.jsonl").write_text("".join(json.dumps({"term": t, "kind": "unverified_name"}) + "\n"
                                                               for t in ["Products", "Pixel", "Adjustable", "Stainless", "TUV", "IEC", "Google", "Windows"]), encoding="utf-8")
        result = gs.auto(self.build)
        self.assertEqual(sorted(result["allowed"]), ["Adjustable", "Pixel", "Products", "Stainless"])
        decisions = json.loads((self.build / "review-decisions.json").read_text(encoding="utf-8"))
        self.assertIn(gs.VOCABULARY_VERSION, decisions["allow_rules"])

    def test_sync_picks_up_manual_edit_to_standard_label(self) -> None:
        self.run_overlay()
        vault = self.build / "vault"
        ledger = vault / governance.LEDGERS["ANM"][0]
        ledger.parent.mkdir(parents=True)
        # Item already exists in state as 待内部处理; human changes it directly to standard label 已关闭.
        ledger.write_text("| ANM ID | 发生了什么 | 涉及 | 当前阶段 | 下一责任人 | 最近更新 |\n| --- | --- | --- | --- | --- | --- |\n"
                          "| P-999-ANM-004 | x | 60 页 | 已关闭 | — | 2026-11-24 |\n", encoding="utf-8")
        self.assertEqual(gs.sync(self.build, vault)["taken"], 1)
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-ANM-004"]["status"], "closed")

    def test_collect_accepts_webpage_internal_governance_decisions_v1(self) -> None:
        self.run_overlay()
        webpage_export = {
            "schema": "governance-decisions.v1",
            "mode": "internal",
            "projectId": "P-999",
            "reviewRevision": "20261124-1",
            "decisions": [
                {
                    "id": "P-999-ANM-004",
                    "decision": "apply_fix",
                    "decisionLabel": "已按原图精校",
                    "targetStatus": "closed",
                    "selectedMembers": ["第 1 页|", "第 2 页|"],
                    "excludedMembers": ["第 3 页|"],
                    "values": {},
                    "note": "前两页已修",
                },
                {
                    "id": "P-999-MAT-007",
                    "decision": "escalate_cus",
                    "decisionLabel": "转客户确认",
                    "targetStatus": "escalated_cus",
                    "values": {"cus_question": "请确认尺寸图型号归属"},
                    "note": "",
                },
                {
                    "id": "P-999-ANM-002",
                    "decision": "",
                },
            ],
        }
        result = gs.collect(self.build, webpage_export)
        self.assertEqual(result["problems"], [])
        records, _ = self.run_overlay()
        anm4 = next(r for r in records["ANM"] if r["id"] == "P-999-ANM-004")
        self.assertIn("剩 58 / 60", anm4["row"][2])
        mat7 = next(r for r in records["MAT"] if r["id"] == "P-999-MAT-007")
        self.assertIn("已转 CUS", mat7["row"][2])

    def test_html_table_guard_masks_multiline_tr_and_skips_unrelated_articles(self) -> None:
        import check_article
        import kb_lookup
        vault = self.build / "vault-root"
        yfd = vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-YFD71F.md"
        other = vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-BV100.md"
        yfd.parent.mkdir(parents=True)
        yfd.write_text("---\ntitle: \"产品系列-YFD71F\"\n---\n\n# 产品系列-YFD71F\n\n## 参数\n\n"
                       "<table>\n<tr>\n  <td rowspan=\"2\">DN65</td>\n  <td>292</td>\n</tr>\n"
                       "<tr>\n  <td>35</td>\n</tr>\n</table>\n", encoding="utf-8")
        other.write_text("---\ntitle: \"产品系列-BV100\"\n---\n\n# 产品系列-BV100\n\n## 参数\n\n- 长度：292 mm\n", encoding="utf-8")
        write_json(vault / kb_lookup.GUARDS, {"guards": [{"id": "P-999-CUS-024", "page": "产品系列-YFD71F", "section": "参数",
                                                          "row": "DN65", "column": "PN10", "value": "292", "question": "292？"}]})
        # Multi-line HTML table row is masked cleanly without breaking rowspan="2".
        res_yfd = kb_lookup.pack(vault, ["产品系列-YFD71F#参数"], [], None, False, True, brief=False)
        self.assertIn("rowspan=\"2\"", res_yfd["text"])
        self.assertIn("<td>[待客户核实，不可引用：P-999-CUS-024]</td>", res_yfd["text"])
        self.assertTrue(res_yfd["disputed_values"][0]["located"])

        # Unrelated product pack does NOT carry YFD71F's disputed value and check_article does not falsely flag BV100.
        res_other = kb_lookup.pack(vault, ["产品系列-BV100#参数"], [], None, False, True, brief=False)
        self.assertEqual(res_other["disputed_values"], [])
        pack_md = self.build / "bv100-pack.md"
        pack_md.write_text(res_other["text"], encoding="utf-8")
        write_json(pack_md.with_suffix(".json"), {"disputed_values": res_other["disputed_values"]})
        guards = check_article.disputed_values([pack_md], vault)
        checked_other = check_article.check("BV100 overall length is 292 mm.", res_other["text"], [], [], vault, None, guards)
        self.assertFalse(any("P-999-CUS-024" in r["text"] for r in checked_other["risky"]))
        # But if the article mentions YFD71F and 292 without packing YFD71F, vault-wide guard still catches it!
        checked_leak = check_article.check("YFD71F torque is 292 Nm.", res_other["text"], [], [], vault, None, guards)
        self.assertTrue(any("P-999-CUS-024" in r["text"] for r in checked_leak["risky"]))

    def test_dry_run_preview_and_undo_for_collect_and_auto(self) -> None:
        self.run_overlay()
        # dry-run must not mutate state on disk.
        preview = gs.collect(self.build, [{"id": "P-999-MAT-007", "action": "close", "note": "整组保留"}], dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["preview"]["groups_changed"], 1)
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-MAT-007"]["status"], "action_needed")

        # Real collect records batch_id, and undo reverts it cleanly.
        applied = gs.collect(self.build, [{"id": "P-999-MAT-007", "action": "close", "note": "整组保留"}])
        self.assertTrue(applied["batch_id"])
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-MAT-007"]["status"], "closed")
        reverted = gs.undo(self.build, applied["batch_id"])
        self.assertEqual(reverted["undone"], applied["batch_id"])
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-MAT-007"]["status"], "action_needed")

        # auto + undo restores review-decisions.json cleanly.
        (self.build / "review-queue.jsonl").write_text(json.dumps({"term": "Adjustable", "kind": "unverified_name"}) + "\n", encoding="utf-8")
        auto_res = gs.auto(self.build)
        self.assertEqual(auto_res["allowed"], ["Adjustable"])
        gs.undo(self.build, auto_res["batch_id"])
        self.assertEqual(json.loads((self.build / "review-decisions.json").read_text(encoding="utf-8")).get("allow_terms", []), [])

    def test_scoped_allow_term_and_rule_aware_reopen(self) -> None:
        import guard_blocks
        res = gs.allow_term(self.build, "AcmeSpec", scope="doc:DOC-1")
        self.assertNotIn("problems", res)
        decisions = json.loads((self.build / "review-decisions.json").read_text(encoding="utf-8"))
        vocab = guard_blocks.load_vocabulary({"own_terms": [], "vocabularies": []}, decisions)
        doc_vocabs = guard_blocks.load_doc_scoped_vocabulary(decisions)
        blk1 = {"doc_id": "DOC-1", "kind": "text", "group": "s1", "text": "AcmeSpec supported", "ocr": True}
        blk2 = {"doc_id": "DOC-2", "kind": "text", "group": "s1", "text": "AcmeSpec supported", "ocr": True}
        _, _, u1 = guard_blocks.scan_block(blk1, None, None, set(), [], vocab, doc_vocabs.get("DOC-1"))
        _, _, u2 = guard_blocks.scan_block(blk2, None, None, set(), [], vocab, doc_vocabs.get("DOC-2"))
        self.assertNotIn("AcmeSpec", u1)
        self.assertIn("AcmeSpec", u2)

        # Rule-aware reopen: project-scoped risk_retained absorbs same-class additions without reopening.
        self.run_overlay()
        gs.collect(self.build, [{"id": "P-999-ANM-002", "action": "risk_retained", "scope": "project", "note": "AI识别页均保留⚠"}])
        more = findings()
        more["ANM"][0]["details"].append("- [[画册#第 99 页|第 99 页]]")
        gs.overlay(self.build, more)
        self.assertEqual(gs.load_state(self.build)["items"]["P-999-ANM-002"]["status"], "risk_retained")

    def test_risk_drawer_exports_full_legacy_evidence(self) -> None:
        records = findings()
        detail = "按原图核对表头、孔型及公差列。" * 14 + "已恢复完整尺寸表。"
        text = f"[[画册#第 15 页|画册 · 第 15 页]]：{detail}"
        risk = records["ANM"][1]
        risk["details"][16] = "- " + text
        gs.overlay(self.build, records)
        state = gs.load_state(self.build)
        item = state["items"][risk["id"]]
        item["status"] = "risk_retained"
        key = gs.member_key(text)
        item["members"][key]["text"] = text[:160]
        gs.save_state(self.build, state)
        next(member for member in risk["members"] if member["key"] == key)["text"] = text[:160]
        write_json(self.build / "governance.json", records)

        exported = json.loads(gs.export_internal_webpage_json(self.build).read_text(encoding="utf-8"))
        drawer = next(record for record in exported["riskRetained"] if record["id"] == risk["id"])
        self.assertEqual(len(drawer["members"]), 60)
        self.assertIn({"label": "画册 · 第 15 页", "detail": detail}, drawer["members"])
        self.assertEqual(gs.load_state(self.build), state)

    def test_webpage_json_exports_and_feedback_json_import(self) -> None:
        import subprocess
        self.run_overlay()
        write_json(self.build / "routes.json", {
            "doc1": {"display_name": "画册.pdf", "source_relpath": "01_画册/画册.pdf"},
        })
        internal_json = gs.export_internal_webpage_json(self.build)
        internal_data = json.loads(internal_json.read_text(encoding="utf-8"))
        self.assertEqual(internal_data["schema"], "internal-review-input.v1")
        self.assertEqual(len(internal_data["issues"]), 2)
        self.assertEqual(len(internal_data["riskRetained"]), 1)

        # Long member text (>200 chars) must not be truncated and splits wiki-link title from full detail.
        long_tail = "此前仅提取到残缺表头，现已对照原图逐格恢复并复核完毕，完整保留全部孔型与公差列。"
        long_line = f"- [[产品画册-YFZ41T#第 15 页|产品画册-YFZ41T · 第 15 页]]：已逐图恢复两张完整尺寸表及各组三行示意图。孔型图标按原页图例改用原文Blind hole thread、Through hole、Drilling through作列头；T后两图标共用一格，2--0等与末列-2/-4均保留原字。{long_tail}"
        long_records = findings()
        long_records["ANM"][1]["details"] = [long_line]
        gs.overlay(self.build, long_records)
        write_json(self.build / "governance.json", {k: [{x: y for x, y in r.items() if x != "details"} for r in v]
                                                    for k, v in long_records.items()})
        long_json = json.loads(gs.export_internal_webpage_json(self.build).read_text(encoding="utf-8"))
        anm4 = next(i for i in long_json["issues"] if i["id"] == "P-999-ANM-004")
        self.assertEqual(anm4["members"][0]["label"], "产品画册-YFZ41T · 第 15 页")
        self.assertTrue(anm4["members"][0]["detail"].endswith(long_tail))

        exported = gs.cus_export(self.build, None)
        chosen = gs.load_state(self.build)["cus_batches"][exported["batch"]]["items"]
        customer_json = gs.export_customer_webpage_json(self.build, exported["batch"], chosen)
        customer_data = json.loads(customer_json.read_text(encoding="utf-8"))
        self.assertEqual(customer_data["schema"], "customer-review-input.v1")
        self.assertTrue(any(i["kind"] == "usage" for i in customer_data["issues"]))
        self.assertTrue(any(i["kind"] == "fact" for i in customer_data["issues"]))

        # If sibling customer-confirmation-webpage skill is present, validate end-to-end HTML build!
        webpage_builder = ROOT.parent / "customer-confirmation-webpage" / "scripts" / "build_review_page.py"
        if webpage_builder.is_file():
            r_int = subprocess.run([sys.executable, str(webpage_builder), "--mode", "internal",
                                    "--data", str(internal_json), "--output", str(self.build / "内部决策" / "int.html")],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(r_int.returncode, 0, r_int.stderr)
            r_cus = subprocess.run([sys.executable, str(webpage_builder), "--mode", "customer",
                                    "--data", str(customer_json), "--output", str(self.build / "交付客户" / "cus.html")],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(r_cus.returncode, 0, r_cus.stderr)

        # apply_answers.py reads customer-feedback.v1 JSON natively.
        run_root = Path(self.temp.name) / "run-apply"
        build_dir = run_root / "04_知识库构建"
        build_dir.mkdir(parents=True)
        cfg = run_root / "kb-config.json"
        write_json(cfg, {"project_id": "P-999"})
        write_json(build_dir / "governance.json", json.loads((self.build / "governance.json").read_text(encoding="utf-8")))
        write_json(build_dir / gs.STATE, gs.load_state(self.build))
        feedback = run_root / "feedback.json"
        write_json(feedback, {
            "schema": "customer-feedback.v1",
            "projectId": "P-999",
            "reviewRevision": exported["batch"],
            "reviewer": {"name": "王工", "team": "研发部"},
            "answers": [
                {"id": "P-999-CUS-007", "answerId": "P-999-CUS-007.1", "decision": "whole", "answerText": "可以公开使用"},
                {"id": "P-999-CUS-006", "answerId": "P-999-CUS-006.1", "decision": "revise", "values": {"correct_value": "Model-A"},
                 "answerText": "按我填写的内容修改：Model-A"},
            ],
        })
        res = subprocess.run([sys.executable, str(ROOT / "scripts" / "apply_answers.py"),
                              "--out", str(run_root), "--config", str(cfg), "--answers", str(feedback)],
                             capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out_json = json.loads(res.stdout)
        self.assertEqual(out_json["applied"], 1)
        self.assertEqual(out_json["notes_for_people"], 2)  # 1 revise answer + 1 reviewer audit note on whole approval.
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["client_answers"]["doc1"], "keep")
        self.assertEqual(gs.load_state(build_dir)["items"]["P-999-CUS-007"]["status"], "answered")

    def test_dimension_drawing_grouping_and_migrate(self) -> None:
        # Three drawings with different model evidence levels split into 3 MAT groups.
        write_json(self.build / "page-check" / "results.json", {
            "i1|page 1": {"item_id": "i1", "unit": "page", "page": 1, "dimension_drawing": True, "drawing_model_confirmed": False},
            "i2|page 1": {"item_id": "i2", "unit": "page", "page": 1, "dimension_drawing": True, "drawing_model_confirmed": False},
            "i3|page 1": {"item_id": "i3", "unit": "page", "page": 1, "dimension_drawing": True, "drawing_model_confirmed": False},
        })
        docs = {
            "DOC-1": {"doc_id": "DOC-1", "item_id": "i1", "title": "规格书-DS50", "file_name": "规格书-DS50.md", "tier": "T1"},
            "DOC-2": {"doc_id": "DOC-2", "item_id": "i2", "title": "其他-尺寸图集", "file_name": "其他-尺寸图集.md", "tier": "T1"},
            "DOC-3": {"doc_id": "DOC-3", "item_id": "i3", "title": "其他-未命名", "file_name": "其他-未命名.md", "tier": "T1"},
        }
        routes = {
            "i1": {"route": "kb", "display_name": "spec.pdf", "source_relpath": "spec.pdf"},
            "i2": {"route": "kb", "display_name": "X200-drawing.pdf", "source_relpath": "X200/drawing.pdf"},
            "i3": {"route": "kb", "display_name": "misc.pdf", "source_relpath": "misc.pdf"},
        }
        governance.build("P-999", self.build, routes, docs, [], [], {}, [], {}, (True, ""), {})
        gov = json.loads((self.build / "governance.json").read_text(encoding="utf-8"))
        mat_keys = {r["key"] for r in gov["MAT"]}
        self.assertIn("dimension_drawing_model:neighbor_title", mat_keys)
        self.assertIn("dimension_drawing_model:weak_hint", mat_keys)
        self.assertIn("dimension_drawing_model:unknown", mat_keys)

        # migrate upgrades state to v2 and migrates allow_terms into scoped_allow_terms idempotently.
        write_json(self.build / "review-decisions.json", {"allow_terms": ["Pixel"]})
        mig = gs.migrate(self.build, self.build / "vault")
        self.assertEqual(mig["schema"], gs.STATE_SCHEMA)
        self.assertEqual(mig["migrated_allow_rules"], 1)
        self.assertEqual(gs.migrate(self.build, self.build / "vault")["migrated_allow_rules"], 0)


if __name__ == "__main__":
    unittest.main()
