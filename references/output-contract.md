# 输出契约

## 1. 脚本 JSON 输出

### find

```json
{
  "scope": "notes | notes+supplementary-fulltext",
  "query": "...",
  "terms": ["..."],
  "candidates": [
    {
      "rel_path": "02vault/<collection>/<paper>.md",
      "title": "...",
      "identity": "<zotero_key | doi | path:...>",
      "zotero_key": "...",
      "doi": null,
      "aliases": ["..."],
      "tags": ["..."],
      "score": 42.0,
      "matched_terms": ["..."],
      "matched_fields": ["title", "key_finding"],
      "snippet": "...",
      "fulltext": {"present": true, "rel_path": "03fulltext/.../KEY.md", "via": "fulltext_path"},
      "pdf_present": false,
      "warnings": ["FULLTEXT_PATH_MISSING: ..."]
    }
  ],
  "coverage": {
    "notes_scanned": 5, "candidate_count": 1, "with_fulltext": 1,
    "note_only": 0, "truncated": false
  },
  "supplementary_fulltext_hits": [
    {"rel_path": "03fulltext/.../KEY.md", "resolved_note": "02vault/.../x.md",
     "note_resolution": "resolved | unresolved-do-not-count-as-paper", "score": 12.0, "snippet": "..."}
  ],
  "warnings": []
}
```

### resolve

```json
{
  "status": "resolved | ambiguous | not_found",
  "match_kind": "path | zotero_key | doi | title",
  "note": {"rel_path": "...", "title": "...", "identity": "...", "zotero_key": "...", "doi": null},
  "fulltext": {"present": true, "rel_path": "...", "via": "fulltext_path | zotero_key | title",
                "key_match": true, "reciprocal_ok": true},
  "pdf": {"present": true, "path": "...", "source": "frontmatter | sibling"},
  "evidence_levels_available": ["L1_note_only", "L2_fulltext_verified", "L3_pdf_verified"],
  "warnings": []
}
```

```json
{"status": "ambiguous", "candidates": [{"rel_path": "...", "zotero_key": "..."}], "next": "..."}
```

### verify

输入（`--evidence` 文件）：

```json
{"items": [
  {"id": "E1", "claim": "可选：这条证据支持哪句结论",
   "source": "03fulltext/<collection>/<key>.md", "quote": "原文引文", "page": 12},
  {"id": "E2", "note": "<zotero_key 或 note 路径>", "quote": "原文引文"}
]}
```

`source` 与 `note` 二选一：`source` 直接校验某文件；`note` 沿 Note → Fulltext/PDF 身份链校验。

输出：

```json
{
  "items": [
    {"id": "E1", "status": "verified | not_found | unavailable",
     "source": "03fulltext/.../KEY.md",
     "match_kind": "exact | case_insensitive",
     "line_start": 16, "line_end": 16,
     "page": 2, "page_basis": "markdown-marker | pdf | unknown",
     "warnings": ["QUOTE_TOO_SHORT"]}
  ],
  "summary": {"total": 2, "verified": 1, "not_found": 1, "unavailable": 0},
  "read_only": true
}
```

## 2. 答案模板（Markdown）

```markdown
## 结论

<直接回答用户问题。区分「Vault 证据支持什么」与「尚无证据」。>

## 关键证据

| # | 等级 | 结论 | 来源 Note | 全文定位 | PDF 页 |
|---|---|---|---|---|---|
| 1 | L2 fulltext_verified | <一条可核验结论> | `02vault/<collection>/<paper>.md` | `03fulltext/<collection>/<key>.md` L16-L16 | 未验证 |
| 2 | L1 note_only | <笔记支持但未全文核验> | `02vault/<collection>/<paper>.md` | — | — |

> 引文 1（L2）：「<逐字引文>」
> 来源：`03fulltext/<collection>/<key>.md` L16-L16（verify: verified）

## 来源文件

- `02vault/<collection>/<paper>.md`（Analytical Note）
- `03fulltext/<collection>/<key>.md`（Fulltext，本轮实际读取）

## 检索覆盖

- queries：`...`、`...`
- 笔记扫描：N 篇；候选：M 篇；有全文：K 篇；实际核验：J 篇
- 范围：NOTE-FIRST（/ 含补充全文召回）

## 证据缺口

- Fulltext 缺失：<论文>
- PDF 页码未验证：<条目>
- 未检索到：<问题维度>
```

规则：

- 每条精确结论必须有等级；没有等级的「事实」一律降级或删除。
- L2/L3 条目必须能对应到本轮 `verify` 的真实结果；不得引用未校验的引文。
- 「来源文件」只列本轮实际读取过的文件。
- 「检索覆盖」必须能复述 `find`/`resolve` 的统计。
- 「证据缺口」不能为空；如果没有缺口，写「无（本轮范围内）」并说明范围。

## 3. 禁止的输出

- 把 Knowledge 页或旧回答当论文原文引用；
- 用「有研究表明」「据了解」替代具体论文与引文；
- 只有页码没有验证依据（「第 5 页」但未跑 PDF verify）；
- 把 `not_found` 的引文换个说法再用；
- 把 L1 结论写成 L2/L3；
- 把「检索覆盖不足」写成「该领域没有研究」。
