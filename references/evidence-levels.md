# 证据等级与引文规则

## 1. 等级定义

| 等级 | 名称 | 定义 | 可以支持什么 |
|---|---|---|---|
| **L0** | `insufficient` | Vault 内没有可定位证据，或检索未命中 | 只能报告「当前 Vault 证据不足」 |
| **L1** | `note_only` | 结论来自 Analytical Note，未经全文核验 | 相关论文、主题归纳、高层方法比较、研究方向框架 |
| **L2** | `fulltext_verified` | 结论已在本篇 Fulltext 中定位到精确原文（`verify` 返回 verified） | 定义、变量、方法细节、系数、机制、原话 |
| **L3** | `pdf_verified` | 页码/公式/表格/图像已在原 PDF 指定页核验（`page_basis: pdf`） | 页码、公式、图表数值、OCR 疑点 |

等级是**单条结论**的属性，不是整篇回答的属性。一条回答里允许同时出现 L1、L2、L3 与 L0 条目，但必须逐条标注。

**没有 Fulltext 层时**（例如 Agentero 式 `papers/<paper>/NOTES.md` + PDF 的论文单元）：L2 不可用，但 L3 可以独立成立——`verify --note ...` 会直接扫描原 PDF 文本并返回 `page_basis: pdf`。此时「无全文」不是缺陷，不要把 L1 结论写成已全文核验。

## 2. 升降级规则

- 只有 `verify` 返回 `status: verified` 才能标 L2；只读了 Fulltext 但没定位到原句，仍算 L1。
- 只有 `verify` 返回 `page_basis: pdf` 且页码匹配，才能声称 L3 / 页码。
- `page_basis: markdown-marker` 的页码只有在用户 Vault 明确维护页码标记时可用，引用时注明「Markdown 页码标记」。
- Markdown 没有页码标记、`pdftotext` 缺失、PDF 缺失 → 页码一律写「未验证」，不影响 L2 结论本身。
- 引文 `not_found` → 该条证据删除或降级 L1，**不得改写引文去凑匹配**。
- `case_insensitive` 命中只表示大小写不同；中文一般无影响，英文引用必须人工确认大小写不改变含义（专有名词、基因名等）。
- 冲突（`KEY_MISMATCH`、`FULLTEXT_AMBIGUOUS`、页码不一致）→ 停止该条目并报告，不生成结论。

## 3. 引文质量要求

- 引文必须**逐字**来自 Fulltext 或 PDF，长度建议 ≥12 个字符，避免「the results」这类无法定位的碎片。
- 引文必须保留限定语境：`however`、`although`、`not`、`only when`、`conditional`、`hypothesis`、`limitation`、`robustness`、样本与情境限定。
- 不得拼接不同段落、不得省略改变语义、不得把中文笔记翻译回英文充当原句。
- 每一条「作者报告的事实/结论」至少配一条可核验引文；方法细节、系数、阈值同样如此。
- 作者观点、分析者推断、研究建议必须分开写：
  - `paper_fact` + L2/L3 引文；
  - `interpretation` 明确标注「解释/推断」；
  - 研究建议标注为建议，不冒充论文结论。

## 4. 页面与位置

- `verify` 的 `page`（PDF）是 1-based 页码。
- Markdown 的 `line_start/line_end` 用于指路与引用定位，不是论文页码。
- 「页码未知」不阻止 L2 结论；只阻止 L3 与页码声明。
- 公式：能定位原页文本时按 L2 引用；渲染乱码或图像公式必须回到 PDF 原页核对（L3），不得凭 Markdown 猜公式。

## 5. 反幻觉红线

- 不得从中文笔记反向生成外文引文。
- 不得虚构页码、系数、阈值、作者、年份。
- 不得把 Knowledge 页或上一轮回答当作论文原文证据。
- 不得把「检索到相关论文」写成「该论文支持这个结论」。
- 检索失败、全文缺失、工具缺失都必须显式报告为缺口，而不是省略。
