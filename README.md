# Agent Harness 从零复刻（learn-claudecode s01→s20）

跟着 [Learn Claude Code](https://learn.shareai.run/zh/timeline/) 的 20 章渐进式课程，从零手写一个 Agent Harness。

> **一个 while 循环 + 一个工具 = 一个 Agent。** 剩下 19 章，都是在这个循环的**外围**叠加机制。

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
| s08 | Context compaction（上下文压缩） | ⬜ |
| s09 | Durable memory（持久记忆层） | ⬜ |
| s10–s20 | prompt 组装 / 重试 / 任务板 / 后台 / 定时 / 团队 / 协议 / worktree / MCP / 整合 | ⬜ |

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填入你的 DEEPSEEK_API_KEY
python s07_skill_loader.py  # 或任意一章
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
| `demo_skill_trace.py` | — | 演示：技能调用留下的 4 处痕迹 |

## 技能库（s07 起）

技能文件放在 `skills/<name>/SKILL.md`，frontmatter 写 `name` + `description`（描述进 prompt 当目录），正文按需加载：

```
skills/
├── cpp-review/SKILL.md      # C++ 代码审查清单（内存/资源/并发/性能）
└── sql-expert/SKILL.md      # SQL 编写与优化规范（索引/查询/事务）
```

## 环境

- Python 3.13（`.venv`）
- `langchain-openai` / `langchain-core` / `python-dotenv`
- LLM：DeepSeek（`deepseek-chat`）
