# research-vault-retrieval

[![tests](https://github.com/Fengsuid/research-vault-retrieval/actions/workflows/test.yml/badge.svg)](https://github.com/Fengsuid/research-vault-retrieval/actions/workflows/test.yml)

一个**只读**的文献知识检索 Skill：先检索 Vault 里的 Analytical Notes 定位论文（NOTE-FIRST），再按需定向进入对应 Fulltext，必要时回到原 PDF 核验页码，并用脚本对每条引文做**位置校验**。

它是可移植的独立 Skill 包，不依赖 Agentero，也不依赖任何具体客户端；你可以把它复制到任意 agent 的 skills 目录，或放进研究 Vault 的 `.agents/skills/`。

## 组成

```
research-vault-retrieval/
├── SKILL.md                       # 检索协议、证据分级、失败处理（给 agent 读）
├── README.md                      # 本文件
├── references/
│   ├── retrieval-routing.md       # 查询类型 → 起点层 / 证据上限 / 覆盖面
│   ├── evidence-levels.md         # L0–L3 定义、升降级、引文质量、反幻觉红线
│   └── output-contract.md         # JSON 输出、答案模板、批量校验输入格式
├── scripts/
│   └── rv_retrieval.py            # 单文件 CLI（标准库，Python 3.9+，只读）
└── tests/
    └── test_rv_retrieval.py       # 36 个测试：布局识别、CJK、引文校验、只读性
```

## 安装

```bash
git clone https://github.com/Fengsuid/research-vault-retrieval.git
```

然后把整个目录复制到任一 skills 位置即可（`SKILL.md` 的 `name` 与目录名一致）：

```bash
# 研究 Vault 内（推荐：随 Vault 分发，换机器也带得走）
cp -R research-vault-retrieval <VAULT>/.agents/skills/

# 或用户级
cp -R research-vault-retrieval ~/.claude/skills/
cp -R research-vault-retrieval ~/.codex/skills/
```

要求 Python 3.9+。PDF 页码校验额外需要系统 `pdftotext`（Poppler）；缺失时脚本会明确返回 `unavailable`，不会假装验证通过。

## 快速开始

```bash
S="$VAULT/.agents/skills/research-vault-retrieval/scripts/rv_retrieval.py"

# 1. 看家底：布局、笔记/全文数量、配对问题
python3 "$S" --vault "$VAULT" stats

# 2. NOTE-FIRST 检索
python3 "$S" --vault "$VAULT" --json find --query "城市形态 建筑能耗"

# 3. 解析论文身份链（Note → Fulltext → PDF）
python3 "$S" --vault "$VAULT" --json resolve --paper AAAAAAAA

# 4. 引文定位校验（单条）
python3 "$S" --vault "$VAULT" --json verify --note AAAAAAAA \
  --quote "The coefficient is 0.42 in the preferred specification."

# 5. 引文定位校验（批量，答案生成前跑一遍）
python3 "$S" --vault "$VAULT" --json verify --evidence evidence.json

# 6. 页码必须对原 PDF 校验
python3 "$S" --vault "$VAULT" --json verify --source "papers/X/paper.pdf" \
  --quote "..." --page 12
```

## Vault 布局识别

无需配置，自动识别三种常见形态：

| 形态 | 笔记 | 全文 | 说明 |
|---|---|---|---|
| 研究 Vault（ZAWS 式） | `02vault/**/*.md`，frontmatter `type: literature-note` 或 tag `literature-note` | `03fulltext/**/*.md`，`type: literature-fulltext` | 也兼容旧 `fulltext/` |
| Agentero 论文单元 | `papers/<paper>/NOTES.md`（同目录有 PDF / `metadata.json` / `PAPER.md`） | 无 | 标题取 `metadata.json`；直接用 PDF 做 L3 校验 |
| 通用 | frontmatter `type: literature-note` | frontmatter `type: literature-fulltext` | 目录名不限 |

自动排除 `.git`、`.obsidian`、`.trash`、`.meta`、`_index`、`templates`/`模板`、`node_modules` 等目录。解析身份优先 `zotero_key`，其次 DOI，再次路径。

## 证据分级

| 等级 | 名称 | 依据 | 用途 |
|---|---|---|---|
| L1 | `note_only` | Analytical Note | 论文定位、主题归纳、高层比较 |
| L2 | `fulltext_verified` | Fulltext 中 `verify` 命中原文 | 定义、系数、机制、原话 |
| L3 | `pdf_verified` | 原 PDF 指定页 `verify` 命中 | 页码、公式、图表 |

详见 [references/evidence-levels.md](references/evidence-levels.md)。

## 只读与安全

- 四个命令（`find` / `resolve` / `verify` / `stats`）都不写任何文件：无缓存、无索引落盘、不改 Vault。测试里有一条专门对全 Vault 做哈希前后比对。
- 不访问网络、不读 Zotero 数据库、不执行外部代码；PDF 只调用本机 `pdftotext` 读取文本。
- 遇到歧义（重复 key、fulltext 不唯一、标题多匹配）**报告并停止**，不猜。
- 校验失败 = 明确失败：`not_found` / `unavailable` 不会被渲染成通过。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功；verify 表示全部 `verified` |
| 1 | resolve 未找到/歧义；verify 有 `not_found` |
| 2 | 用法或环境错误（Vault 不存在、`pdftotext` 缺失、证据 JSON 非法） |

## 与两个参考项目的关系

- 检索协议（NOTE-FIRST、定向全文、证据分级、综述候选池覆盖要求）参考了 [Zotero-Analytical-Workflow-Skills](https://github.com/cheneternity/Zotero-Analytical-Workflow-Skills) 的 Retrieval Skill 思路，但把**校验从提示词搬进了代码**：`verify` 直接证明引文在哪个文件哪一行/哪一页。
- 写入侧的思路（审批、只读默认、不盲重试）参考了 [research-literature-workflow](https://github.com/linguangsen637-crypto/research-literature-workflow)，但本包是纯只读，写入能力留作下一步。
- 本包不复制两个项目的代码或模板，独立实现，MIT License（见 [LICENSE](LICENSE)）。

## 许可证

MIT License，见 [LICENSE](LICENSE)。仓库自带的 `tests` 与 `.github/workflows/test.yml` 会在 Ubuntu / macOS 的 Python 3.9 与 3.12 上运行。

## 测试

```bash
python3 -m unittest discover -s skills/research-vault-retrieval/tests -v
```

覆盖：notes/fulltext 布局识别、模板与 `.obsidian` 排除、CJK 检索、补充全文召回标注、身份链解析、歧义停止、引文精确/大小写/跨行匹配、页码标记、错误页拒绝、批量证据校验、假 `pdftotext` PDF 页校验、缺工具降级、只读性、退出码。

## 已知限制

- 关键词检索，不做向量/语义检索；中文按整词 + 二元组匹配，同义词需用户或 agent 自行扩展。
- Markdown 页码依赖 Vault 内的页码标记（可用 `--page-marker-regex` 自定义）；无标记时页码必须回到 PDF。
- 不做 OCR；扫描 PDF 需要先 OCR 出文本层。
- 假定笔记/全文是 UTF-8 Markdown；不解析 Obsidian Dataview、嵌入块语法。
- 只读：不生成笔记、不修复链接、不迁移布局。

## 可选的后续步骤

1. **写入型能力**：在 `verify` 通过的前提下，按「计划 + 审批哈希 + 备份 + 回读」写入笔记（只读包保持独立）。
2. **接入 Agentero**（可选）：把本目录复制为 `templates/vault/.agents/skills/research-vault-retrieval/`，即成为新 Vault 的种子 Skill；需按仓库约定补版本号与文档索引。不做也不影响本包使用。
