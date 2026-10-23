"""End-to-end tests for the read-only research-vault-retrieval CLI.

Run:  python3 -m unittest discover -s skills/research-vault-retrieval/tests -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rv_retrieval.py"
sys.path.insert(0, str(SCRIPT.parent))
import rv_retrieval as rv  # noqa: E402

NOTE_A = """---
type: literature-note
title: 城市形态与建筑能耗
aliases: [城市形态—能耗, Urban form and energy]
tags: [literature-note, 城市形态]
zotero_key: AAAAAAAA
pdf_key: PDFAAAAA
fulltext_path: 03fulltext/城市形态/AAAAAAAA.md
theme: 城市形态对建筑能耗的影响
key_finding: 三维形态指标显著影响住宅能耗
methodology: 随机森林与 SHAP
core_variable: 建筑高度、体积与寿命
study_area: 中国 53 个城市
relevance: 可借鉴其指标构造方法
---
# 城市形态与建筑能耗

本文研究城市形态与建筑能耗的关系。三维形态指标显著影响住宅能耗。
"""

FULLTEXT_A = """---
type: literature-fulltext
title: Urban form and energy consumption
zotero_key: AAAAAAAA
pdf_key: PDFAAAAA
note_path: 02vault/城市形态/城市形态与建筑能耗.md
page_mapping: unknown
---
# Urban form and energy consumption

<!-- page: 1 -->
Abstract: This study examines urban form and energy use.

<!-- page: 2 -->
We find that building height, volume and lifespan explain residential
energy use. The coefficient is 0.42 in the preferred specification.

补充召回标记 appears only in this fulltext body.
"""

NOTE_B = """---
type: literature-note
title: 绿色屋顶降温效应
aliases: [Green roof cooling]
tags: [literature-note]
zotero_key: BBBBBBBB
theme: 绿色屋顶
key_finding: 绿色屋顶可降低表面温度
---
# 绿色屋顶降温效应

绿色屋顶可降低表面温度。
"""

NOTE_C1 = """---
type: literature-note
title: 重复键论文一
zotero_key: CCCCCCCC
---
# 重复键论文一

正文一。
"""

NOTE_C2 = """---
type: literature-note
title: 重复键论文二
zotero_key: CCCCCCCC
---
# 重复键论文二

正文二。
"""

NOTES_MD = """---
aliases:
  - A Great Paper: Evidence
  - GP
created: 2026-01-01
---
# A Great Paper

Agentero-style paper notes.
"""

ORPHAN_FULLTEXT = """---
type: literature-fulltext
title: Orphan fulltext
zotero_key: DDDDDDDD
---
# Orphan

孤儿全文独有词仅出现在这里。
"""

TEMPLATE_MD = """---
title: 论文精读模板
---
# 论文精读模板

模板独有占位词，不应参与检索。
"""

PLAIN_MD = """# 随手笔记

obsidian配置词 与文献无关。
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot(root: Path):
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        entries.append((path.relative_to(root).as_posix(), stat.st_size,
                        stat.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest()))
    return entries


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rv-retrieval-test-")
        cls.vault = Path(cls.tmp) / "Vault"
        write(cls.vault / "02vault/城市形态/城市形态与建筑能耗.md", NOTE_A)
        write(cls.vault / "02vault/绿色建筑/绿色屋顶降温效应.md", NOTE_B)
        write(cls.vault / "02vault/重复/重复键论文一.md", NOTE_C1)
        write(cls.vault / "02vault/重复/重复键论文二.md", NOTE_C2)
        write(cls.vault / "03fulltext/城市形态/AAAAAAAA.md", FULLTEXT_A)
        write(cls.vault / "03fulltext/城市形态/ORPHAN.md", ORPHAN_FULLTEXT)
        write(cls.vault / "papers/A Great Paper/NOTES.md", NOTES_MD)
        (cls.vault / "papers/A Great Paper/paper.pdf").write_bytes(b"%PDF-1.4\nfake\n")
        write(cls.vault / "模板/论文精读模板.md", TEMPLATE_MD)
        write(cls.vault / "notes/random.md", PLAIN_MD)
        write(cls.vault / ".obsidian/workspace.md", "# workspace\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault)] + list(args),
            capture_output=True, text=True, env=env,
        )

    def run_json(self, *args, env=None):
        result = self.run_cli(*(("--json",) + args), env=env)
        payload = json.loads(result.stdout) if result.stdout.strip() else None
        return result, payload

    def index(self):
        return rv.VaultIndex(self.vault)

    # -- discovery --------------------------------------------------------
    def test_discovers_notes_by_frontmatter_tag_and_paper_folder(self):
        index = self.index()
        rel_paths = sorted(record.rel_path for record in index.notes)
        self.assertIn("02vault/城市形态/城市形态与建筑能耗.md", rel_paths)
        self.assertIn("02vault/绿色建筑/绿色屋顶降温效应.md", rel_paths)
        self.assertIn("papers/A Great Paper/NOTES.md", rel_paths)
        self.assertEqual(len(index.notes), 5)
        fulltexts = sorted(record.rel_path for record in index.fulltexts)
        self.assertEqual(fulltexts, [
            "03fulltext/城市形态/AAAAAAAA.md",
            "03fulltext/城市形态/ORPHAN.md",
        ])

    def test_ignores_plain_markdown_and_excluded_directories(self):
        index = self.index()
        rel_paths = {record.rel_path for record in index.notes + index.fulltexts}
        self.assertNotIn("notes/random.md", rel_paths)
        self.assertFalse(any(path.startswith(("模板/", ".obsidian/")) for path in rel_paths))

    # -- find (NOTE-FIRST) ------------------------------------------------
    def test_find_searches_notes_only_by_default(self):
        result, payload = self.run_json("find", "--query", "城市形态 建筑能耗")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["scope"], "notes")
        paths = [item["rel_path"] for item in payload["candidates"]]
        self.assertIn("02vault/城市形态/城市形态与建筑能耗.md", paths)
        self.assertFalse(any(path.startswith("03fulltext/") for path in paths))
        self.assertEqual(payload["supplementary_fulltext_hits"], [])

    def test_find_supports_cjk_terms(self):
        _, payload = self.run_json("find", "--query", "建筑能耗")
        paths = [item["rel_path"] for item in payload["candidates"]]
        self.assertIn("02vault/城市形态/城市形态与建筑能耗.md", paths)

    def test_find_surfaces_fulltext_only_when_explicitly_requested(self):
        _, default_payload = self.run_json("find", "--query", "补充召回标记")
        self.assertEqual(default_payload["candidates"], [])

        _, payload = self.run_json("find", "--query", "补充召回标记", "--include-fulltext")
        self.assertEqual(payload["scope"], "notes+supplementary-fulltext")
        hits = payload["supplementary_fulltext_hits"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["rel_path"], "03fulltext/城市形态/AAAAAAAA.md")
        self.assertEqual(hits[0]["resolved_note"], "02vault/城市形态/城市形态与建筑能耗.md")

    def test_find_labels_unresolved_fulltext_hits(self):
        _, payload = self.run_json("find", "--query", "孤儿全文独有词", "--include-fulltext")
        self.assertEqual(payload["candidates"], [])
        hits = payload["supplementary_fulltext_hits"]
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0]["resolved_note"])
        self.assertEqual(hits[0]["note_resolution"], "unresolved-do-not-count-as-paper")

    def test_find_excludes_templates_and_obsidian_state(self):
        _, template_payload = self.run_json("find", "--query", "模板独有占位词")
        self.assertEqual(template_payload["candidates"], [])
        _, obsidian_payload = self.run_json("find", "--query", "obsidian配置词")
        self.assertEqual(obsidian_payload["candidates"], [])

    def test_find_output_has_no_match_guidance(self):
        result = self.run_cli("find", "--query", "不存在的主题关键词")
        self.assertEqual(result.returncode, 0)
        self.assertIn("NO_MATCH", result.stdout)

    # -- resolve ----------------------------------------------------------
    def test_resolve_by_zotero_key_returns_note_fulltext_chain(self):
        result, payload = self.run_json("resolve", "--paper", "AAAAAAAA")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["match_kind"], "zotero_key")
        self.assertEqual(payload["note"]["rel_path"], "02vault/城市形态/城市形态与建筑能耗.md")
        self.assertTrue(payload["fulltext"]["present"])
        self.assertEqual(payload["fulltext"]["via"], "fulltext_path")
        self.assertTrue(payload["fulltext"]["key_match"])
        self.assertTrue(payload["fulltext"]["reciprocal_ok"])
        self.assertIn("L2_fulltext_verified", payload["evidence_levels_available"])

    def test_resolve_by_title_reports_missing_fulltext(self):
        result, payload = self.run_json("resolve", "--paper", "绿色屋顶降温效应")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "resolved")
        self.assertFalse(payload["fulltext"]["present"])
        self.assertEqual(payload["evidence_levels_available"], ["L1_note_only"])

    def test_resolve_agentero_notes_md_by_alias(self):
        result, payload = self.run_json("resolve", "--paper", "GP")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["note"]["rel_path"], "papers/A Great Paper/NOTES.md")
        self.assertTrue(payload["pdf"]["present"])
        self.assertEqual(payload["pdf"]["source"], "sibling")

    def test_resolve_ambiguous_identity_stops_without_guessing(self):
        result, payload = self.run_json("resolve", "--paper", "CCCCCCCC")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertIn("do not guess", payload["next"])

    def test_resolve_not_found_is_explicit(self):
        result, payload = self.run_json("resolve", "--paper", "DDDDDDDD")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "not_found")

    # -- verify: Markdown -------------------------------------------------
    def test_verify_markdown_quote_with_page_marker(self):
        result, payload = self.run_json(
            "verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
            "--quote", "building height, volume and lifespan explain residential energy use",
        )
        self.assertEqual(result.returncode, 0)
        item = payload["items"][0]
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["match_kind"], "exact")
        self.assertEqual(item["page"], 2)
        self.assertEqual(item["page_basis"], "markdown-marker")
        self.assertGreaterEqual(item["line_start"], 1)
        self.assertLessEqual(item["line_start"], item["line_end"])

    def test_verify_whitespace_and_linebreak_variants(self):
        _, payload = self.run_json(
            "verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
            "--quote", "building height, volume and\nlifespan   explain residential energy use",
        )
        self.assertEqual(payload["items"][0]["status"], "verified")

    def test_verify_detects_case_insensitive_match(self):
        _, payload = self.run_json(
            "verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
            "--quote", "THE COEFFICIENT IS 0.42",
        )
        self.assertEqual(payload["items"][0]["status"], "verified")
        self.assertEqual(payload["items"][0]["match_kind"], "case_insensitive")

    def test_verify_rejects_wrong_quote(self):
        result, payload = self.run_json(
            "verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
            "--quote", "this sentence does not exist in the source",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["items"][0]["status"], "not_found")

    def test_verify_rejects_wrong_page(self):
        result, payload = self.run_json(
            "verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
            "--quote", "We find that building height",
            "--page", "1",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["items"][0]["status"], "not_found")
        self.assertIn("page 2", payload["items"][0]["reason"])

    def test_verify_note_resolves_fulltext_chain(self):
        result, payload = self.run_json(
            "verify", "--note", "AAAAAAAA",
            "--quote", "The coefficient is 0.42 in the preferred specification.",
        )
        self.assertEqual(result.returncode, 0)
        item = payload["items"][0]
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["source"], "03fulltext/城市形态/AAAAAAAA.md")
        self.assertEqual(item["note"], "02vault/城市形态/城市形态与建筑能耗.md")

    def test_verify_note_without_fulltext_is_unavailable(self):
        result, payload = self.run_json(
            "verify", "--note", "BBBBBBBB", "--quote", "绿色屋顶可降低表面温度",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["items"][0]["status"], "unavailable")

    # -- verify: batch evidence ------------------------------------------
    def test_verify_evidence_batch(self):
        evidence = {
            "items": [
                {"id": "E1", "claim": "全文有原句",
                 "source": "03fulltext/城市形态/AAAAAAAA.md",
                 "quote": "building height, volume and lifespan explain residential energy use"},
                {"id": "E2", "claim": "引文不存在",
                 "source": "03fulltext/城市形态/AAAAAAAA.md",
                 "quote": "a fabricated quotation that is not in the paper"},
            ]
        }
        evidence_path = Path(self.tmp) / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        result, payload = self.run_json("verify", "--evidence", str(evidence_path))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["summary"], {
            "total": 2, "verified": 1, "not_found": 1, "unavailable": 0,
        })
        self.assertEqual(payload["items"][0]["status"], "verified")
        self.assertEqual(payload["items"][1]["status"], "not_found")

    def test_verify_evidence_note_reference(self):
        evidence = {"items": [{"id": "E1", "note": "AAAAAAAA",
                               "quote": "The coefficient is 0.42"}]}
        evidence_path = Path(self.tmp) / "evidence-note.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        result, payload = self.run_json("verify", "--evidence", str(evidence_path))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["summary"]["verified"], 1)

    # -- verify: PDF ------------------------------------------------------
    def test_verify_pdf_page_with_fake_pdftotext(self):
        bin_dir = Path(self.tmp) / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "pdftotext"
        fake.write_text(
            "#!/bin/sh\nprintf 'Page one text.\\n\\fPage two contains the verified phrase of the paper.\\n'\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        result, payload = self.run_json(
            "verify", "--source", "papers/A Great Paper/paper.pdf",
            "--quote", "the verified phrase of the paper", "--page", "2", env=env,
        )
        self.assertEqual(result.returncode, 0)
        item = payload["items"][0]
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["page"], 2)
        self.assertEqual(item["page_basis"], "pdf")

    def test_verify_pdf_reports_missing_tool_instead_of_passing(self):
        env = {"PATH": ""}
        result, payload = self.run_json(
            "verify", "--source", "papers/A Great Paper/paper.pdf",
            "--quote", "the verified phrase of the paper", env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["items"][0]["status"], "unavailable")

    # -- stats ------------------------------------------------------------
    def test_stats_reports_coverage_and_issues(self):
        result, payload = self.run_json("stats")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["counts"]["notes"], 5)
        self.assertEqual(payload["counts"]["fulltexts"], 2)
        self.assertEqual(payload["coverage"]["notes_with_fulltext"], 1)
        self.assertEqual(payload["coverage"]["notes_note_only"], 4)
        kinds = set(payload["issue_counts"])
        self.assertIn("FULLTEXT_MISSING", kinds)
        self.assertIn("FULLTEXT_WITHOUT_NOTE", kinds)
        self.assertIn("DUPLICATE_NOTE_KEY", kinds)
        self.assertEqual(payload["issue_counts"]["DUPLICATE_NOTE_KEY"], 1)

    # -- safety -----------------------------------------------------------
    def test_all_commands_are_read_only(self):
        before = snapshot(self.vault)
        self.run_cli("find", "--query", "城市形态")
        self.run_cli("find", "--query", "补充召回标记", "--include-fulltext")
        self.run_cli("resolve", "--paper", "AAAAAAAA")
        self.run_cli("resolve", "--paper", "CCCCCCCC")
        self.run_cli("verify", "--source", "03fulltext/城市形态/AAAAAAAA.md",
                     "--quote", "The coefficient is 0.42")
        self.run_cli("stats")
        after = snapshot(self.vault)
        self.assertEqual(before, after)

    def test_missing_vault_is_usage_error(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(Path(self.tmp) / "nope"),
             "--json", "stats"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")

    def test_verify_requires_quote_and_target(self):
        result = self.run_cli("verify", "--quote", "something")
        self.assertEqual(result.returncode, 2)


class UnitTests(unittest.TestCase):
    def test_query_terms_keep_cjk_bigrams(self):
        terms = dict(rv.query_terms("建筑能耗"))
        self.assertIn("建筑能耗", terms)
        self.assertIn("建筑", terms)

    def test_frontmatter_parses_inline_and_block_lists(self):
        fields, body, start = rv.parse_frontmatter(
            "---\ntitle: X\naliases: [A, 'B C']\ntags:\n  - literature-note\n  - urban\n---\n# Body\n"
        )
        self.assertEqual(fields["title"], "X")
        self.assertEqual(fields["aliases"], ["A", "B C"])
        self.assertEqual(fields["tags"], ["literature-note", "urban"])
        self.assertIn("# Body", body)
        self.assertEqual(start, 8)

    def test_quote_lookup_normalizes_whitespace(self):
        hit = rv.find_quote_in_text("alpha beta\ngamma delta", "alpha   beta gamma")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["match_kind"], "exact")

    def test_custom_page_marker_regex(self):
        pattern = r"^\s*@@\s*(\d+)\s*$"
        result = rv.verify_markdown(
            self._write_temp("@@ 4\nquoted phrase that is long enough\n"),
            "quoted phrase that is long enough", None, pattern,
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["page"], 4)

    def _write_temp(self, text: str) -> Path:
        import tempfile as _tempfile
        handle = _tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        return Path(handle.name)


class AgenteroLayoutTests(unittest.TestCase):
    """PDF-only paper units: papers/<topic>/<id>/{NOTES.md, metadata.json, PDF}."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rv-agentero-test-")
        cls.vault = Path(cls.tmp) / "Vault"
        write(cls.vault / "papers/optimization/1412.6980/NOTES.md",
              "---\naliases:\n  - Adam\n---\n# NOTES — Adam\n\n自适应学习率与偏差校正。\n")
        write(cls.vault / "papers/optimization/1412.6980/metadata.json", json.dumps({
            "id": "1412.6980", "title": "Adam: A Method for Stochastic Optimization",
            "authors": ["Diederik P. Kingma"], "year": 2015,
        }, ensure_ascii=False))
        (cls.vault / "papers/optimization/1412.6980/1412.6980.pdf").write_bytes(b"%PDF-1.4\nfake\n")
        write(cls.vault / "papers/vision/1512.03385/NOTES.md",
              "# NOTES — ResNet\n\n残差连接。\n")
        write(cls.vault / "papers/vision/1512.03385/metadata.json", json.dumps({
            "id": "1512.03385", "title": "Deep Residual Learning for Image Recognition",
        }, ensure_ascii=False))
        (cls.vault / "papers/vision/1512.03385/1512.03385.pdf").write_bytes(b"%PDF-1.4\nfake\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault)] + list(args),
            capture_output=True, text=True, env=env,
        )

    def run_json(self, *args, env=None):
        result = self.run_cli(*(("--json",) + args), env=env)
        return result, json.loads(result.stdout) if result.stdout.strip() else None

    def fake_pdftotext_env(self):
        bin_dir = Path(self.tmp) / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "pdftotext"
        fake.write_text(
            "#!/bin/sh\nprintf 'Page one.\\n\\fPage two contains the verified phrase.\\n'\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env

    def test_stats_agentero_layout_does_not_report_missing_fulltext(self):
        result, payload = self.run_json("stats")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(payload["layout_guess"].startswith("agentero"))
        self.assertEqual(payload["counts"]["notes"], 2)
        self.assertEqual(payload["counts"]["pdf_resolvable"], 2)
        self.assertNotIn("FULLTEXT_MISSING", payload["issue_counts"])
        self.assertEqual(payload["coverage"]["notes_pdf_verifiable"], 2)

    def test_title_comes_from_metadata_json_in_paper_folder(self):
        _, payload = self.run_json("find", "--query", "Adam stochastic optimization")
        titles = [item["title"] for item in payload["candidates"]]
        self.assertIn("Adam: A Method for Stochastic Optimization", titles)

    def test_resolve_agentero_paper_has_pdf_evidence_without_fulltext(self):
        result, payload = self.run_json("resolve", "--paper", "Adam")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(payload["fulltext"]["present"])
        self.assertTrue(payload["pdf"]["present"])
        self.assertEqual(payload["pdf"]["source"], "sibling")
        self.assertIn("L1_note_only", payload["evidence_levels_available"])
        self.assertNotIn("L2_fulltext_verified", payload["evidence_levels_available"])

    def test_verify_note_scans_pdf_when_no_fulltext_layer(self):
        result, payload = self.run_json(
            "verify", "--note", "papers/optimization/1412.6980/NOTES.md",
            "--quote", "verified phrase", env=self.fake_pdftotext_env(),
        )
        self.assertEqual(result.returncode, 0)
        item = payload["items"][0]
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["page"], 2)
        self.assertEqual(item["page_basis"], "pdf")
        self.assertTrue(any("PDF_ONLY" in warning for warning in item["warnings"]))


if __name__ == "__main__":
    unittest.main()
