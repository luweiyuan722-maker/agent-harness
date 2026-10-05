# Agent Harness 从零复刻（learn-claudecode s01→s20）

跟着 [Learn Claude Code](https://learn.shareai.run/zh/timeline/) 的 20 章渐进式课程，从零手写一个 Agent Harness。

> **一个 while 循环 + 一个工具 = 一个 Agent。** 剩下 19 章，都是在这个循环的**外围**叠加机制。

## 这是一个「通用 Harness」，不是某个领域的工具

核心是**领域无关**的：一个 model/tool 循环 + 外围机制（权限、钩子、计划、子代理、技能、压缩）。

演示里用 **C++ 代码审查** 当例子，只是因为"审查代码"能自然触发**多步工具调用 + 专业技能注入**，最能体现各层机制在真实场景下的配合。换成别的领域**不需要改任何代码**，只要加一个技能文件：

| 想要的能力 | 需要做的事 | 状态 |
|---|---|---|
| C++ 代码审查（内存/资源/并发/性能） | `skills/cpp-review/SKILL.md` | 已内置 |
| SQL 编写与优化（索引/查询/事务） | `skills/sql-expert/SKILL.md` | 已内置 |
| Python / Go / Rust 代码审查 | 换个清单写 `skills/<name>/SKILL.md` | 加文件即可 |
| 数据分析、写作、运维排障、文档问答… | 同上 | 加文件即可 |

工具层同样是通用的——`run_bash` 能执行的任何事，Agent 都能编排成多步任务。

## 进度

| # | 章节 | 状态 |
|:--:|---|:--:|
| s01 | Agent Loop（最小 model/tool 循环） | ✅ |
| s02 | Tool dispatch（工具分发表 · 单一数据源） | ✅ |
| s03 | Permission gate（执行前权限门） | ✅ |
| s04 | Lifecycle hooks（生命周期钩子） | ✅ |
| s05 | Todo manager（计划管理） | ✅ |
| s06 | Subagent（子代理） | ✅ |
| s07 | Skill loader（按需加载技能） | ✅ |
| s08 | Context compaction（上下文压缩六件套） | ✅ |
| s09 | Durable memory（持久记忆层） | ✅ |
| s10 | Runtime prompt assembly（运行时组装 prompt） | ✅ |
| s11 | Retry strategy（重试策略） | ✅ |
| s12 | Task board（任务系统：依赖图 + 持久化） | ✅ |
| s13–s20 | 后台执行 / 定时 / 团队 / 协议 / 自主认领 / worktree / MCP / 整合 | ⬜ |

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填入你的 DEEPSEEK_API_KEY
python s08_context_compact.py   # 或任意一章
```

## 各章文件

| 文件 | 章节 | 核心机制 |
|---|---|---|
| `s01_agent_loop.py` | s01 | 最小循环：调模型 → 执行工具 → 结果喂回 |
| `s02_tool_dispatch.py` | s02 | 工具列表单一数据源 + 名字→函数分派表 |
| `s03_permission_gate.py` | s03 | 危险工具执行前的决策点（拒绝时喂回，不中断） |
| `s04_lifecycle_hooks.py` | s04 | 钩子系统：日志/计时挂在循环周围，不写进循环 |
| `s05_todo_manager.py` | s05 | 计划存在循环外的变量，`todo_write` 工具写入 + 每轮注入 prompt |
| `s06_subagent.py` | s06 | 子 agent = 独立循环 + 独立 messages，包装成 `delegate` 工具（主/子工具分离防递归） |
| `s07_skill_loader.py` | s07 | 技能目录进 prompt，正文用 `load_skill` 按需注入（省 92% 上下文） |
| `s08_context_compact.py` | s08 | 上下文压缩六件套（见下） |
| `s09_memory_full.py` | s09 | 持久记忆层：文件仓库 + 索引 + 按需注入 + 提取 + 去重（见下） |
| `s10_system_prompt.py` | s10 | 运行时组装 system prompt：PROMPT_SECTIONS 分段 + assemble/get_system_prompt + update_context（见下） |
| `s11_retry_strategy.py` | s11 | 错误恢复：RecoveryState + 三判断函数 + 指数退避 + 三条恢复路径（见下） |
| `s12_task_system.py` | s12 | 任务系统：Task DAG（blockedBy 依赖 + owner 分工）+ .tasks/ 持久化 + 状态机（见下） |
| `demo_skill_trace.py` | — | 演示：技能调用留下的 4 处痕迹 |

## s08：上下文压缩六件套

**核心原则：便宜的先跑，贵的后跑。** 前三层 0 次 API 调用，只有后两层才调 LLM。

| 层 | 触发条件 | 动作 | 成本 |
|:--:|---|---|:--:|
| 度量 `estimate_chars` | 每轮 | 估算字符数（序列化整条消息，不漏 `tool_calls`） | 0 |
| **L3** `tool_result_budget` | 工具结果总量 > 200k | 大结果**落盘**，上下文留路径 + 预览（信息不丢） | 0 |
| **L1** `snip_compact` | 消息数 > 50 | 保留头 3 + 尾 47，中间用 marker 替代 | 0 |
| **L2** `micro_compact` | 上下文 > 50k | 模型**已看过**的旧工具结果 → 占位符 | 0 |
| **L4** `compact_history` | 前三层后仍超限 | **LLM 全量摘要**成一条状态摘要 | 1 API |
| **应急** `reactive_compact` | API 报 `prompt_too_long` | 保住最新 5 条，其余摘要 | 1 API |

**三个关键设计**：

1. **顺序即设计** —— 执行顺序是 `L3 → L1 → L2 → L4`（与编号不同）。L3 必须最先跑：大结果先落盘，L2 才敢放心把旧结果换成占位符。
2. **两条线** —— 触发线（`CONTEXT_CHAR_LIMIT`，超了才动手）与达标线（`target = LIMIT × 0.8`，压到这就收手）是**两个不同的数**。
3. **占位符要继承恢复信息** —— 被 L2 替换的若是 L3 的落盘引用，占位符必须**保留文件路径**，否则"信息不丢"的承诺会被 L2 打破。

## s09：持久记忆层（Durable Memory）

**核心原则：压缩会丢细节，要有一层不丢的。** 记忆活过压缩、也活过会话。

| 模块 | 机制 | 说明 |
|:--:|---|---|
| 存储 | `.memory/<slug>.md` | 一条记忆一个文件，frontmatter（name/description/type）+ 正文带 **Why/How** |
| 索引 | `MEMORY.md` | 一行一个链接，**常驻 SYSTEM prompt**（轻、稳定 → prompt cache 命中） |
| 按需 | side-query | LLM 轻量筛选相关记忆，正文**注入 user turn**（不污染 SYSTEM 的 cache） |
| 降级 | bigram 关键词 | side-query 挂了 → 2 字滑动窗口本地匹配（中文分词近似） |
| 写入 | `extract_memories` | 任务结束（无 tool_calls）时提取，不在每轮（防记半成品想法） |
| 去重 | `find_duplicate` | 写入前 LLM 查重，重复则**更新**不新建（同义不同措辞也能识别） |

**四类记忆**：`user`（你是谁）/ `feedback`（怎么做事）/ `project`（在发生什么）/ `reference`（东西在哪找）。

**两条加载路径（prompt cache 友好）**：索引（稳定）放 SYSTEM，正文（多变）放 user turn —— 稳定的放前面保住 cache 前缀，多变的放后面不破坏它。

**两个踩过的坑**：① `str.format()` 撞上 prompt 里的 JSON 花括号 → `KeyError`，改用 `replace`；② 中文整段子串匹配失效（"内存管理用"匹配不到"内存泄漏"）→ 用 2 字 bigram 近似分词。

## s10：System Prompt（运行时组装）

**核心原则：prompt 是组装出来的，不是写死的。** 把硬编码的 SYSTEM 拆成 section，运行时按真实状态拼接。

| 组件 | 机制 |
|------|------|
| `PROMPT_SECTIONS` | 四段字典：identity / tools / workspace / memory |
| `assemble_system_prompt` | 始终 3 段 + 按需 memory 段（空行分隔） |
| `get_system_prompt` | `json.dumps` 做 key，context 不变返回缓存 |
| `update_context` | 查真实状态（文件/工具），不猜关键词 |

**关键设计**：
1. **tools 段动态生成**（`', '.join(enabled_tools)`），不写死 —— 工具变了 prompt 自动跟着变。
2. **memory 段基于 `INDEX_FILE.exists()` 真实状态**，不是关键词匹配。
3. **缓存 key 用 `json.dumps` 不用 `hash()`**（dict 不可哈希 + 字符串哈希进程随机化，不稳定）。

## s11：Error Recovery（错误恢复）

**核心原则：错误不是终点，是重试的起点。** 把 LLM 调用包进 try/except，按错误类型走三条恢复路径。

| 故障 | 触发 | 恢复 | 上限 |
|------|------|------|------|
| 输出截断 | `finish_reason == length` | 升级 max_tokens 8K→64K / 续写提示 | 升级 1 次 + 续写 3 次 |
| 上下文超限 | `prompt_too_long` | reactive compact | 压缩 1 次 |
| 临时故障 | 429 / 529 | 指数退避 + 抖动，连续 529 切备用模型 | 退避 10 次 |

**设计哲学**：确定性故障（截断/超限）改条件重试 1 次就够；随机性故障（限流/过载）指数退避多次重试。判断标准 = **「重试能不能改变结果」**。`RecoveryState` 记账防死循环。

## s12：任务系统（Task System）

> ⚠️ 章节编号：网站 learn.shareai.run 用 s12，GitHub 仓库用 s10_task_system（两套编号，内容相同）。

**核心原则：大目标拆成小任务，排好序，持久化。** 相比 s05 TodoWrite，任务有了 ID、依赖（`blockedBy`）和分工（`owner`），并持久化到 `.tasks/{id}.json` 跨会话可恢复。

| 组件 | 机制 |
|------|------|
| `Task` | dataclass 六字段：id / subject / description / status / owner / blockedBy |
| `TaskStore` | 校验 ID + 读写 JSON + 排他写入（`open("x")`）+ 路径安全 + 防环 |
| 状态机 | `pending ──claim──→ in_progress ──complete──→ completed` |
| `create_task` / `update_task` | 创建节点 / 加依赖边（两阶段构建） |
| `claim_task` / `complete_task` | 认领（依赖全完成才行）/ 完成 + 解锁下游 |

**三个关键设计**：
1. **两阶段构建** —— 先 create 所有节点拿运行时 ID，再 update 加边：同级工具调用无法引用另一个调用刚生成的 ID。
2. **防环** —— `_depends_on` 用 BFS 检测传递依赖，防止 `A→B→A`。
3. **路径安全** —— `TASK_ID_PATTERN` 校验 ID 格式 + `resolve()`/`is_relative_to` 防路径注入/逃逸。

## 技能库（s07 起）

技能文件放在 `skills/<name>/SKILL.md`，frontmatter 写 `name` + `description`（描述进 prompt 当目录），正文按需加载：

```
skills/
├── cpp-review/SKILL.md      # C++ 代码审查清单（内存/资源/并发/性能）
└── sql-expert/SKILL.md      # SQL 编写与优化规范（索引/查询/事务）
```

> 加一个目录就是加一项能力 —— 这是「通用 harness」最直观的体现。

## 环境

- Python 3.13（`.venv`）
- `langchain-openai` / `langchain-core` / `python-dotenv`
- LLM：DeepSeek（`deepseek-chat`）

> 运行时会生成 `.task_outputs/`（工具结果归档）、`.transcripts/`（历史留底）和 `.memory/`（持久记忆库，属于用户数据）—— 均已在 `.gitignore` 中排除。
