"""s20: Comprehensive Agent Turn —— 机制很多，循环一个

课程：learn-claudecode s20（终点章）
主旨：把 s01-s19 的机制合回同一个 while True 循环。

循环本身从未变过：
  while True:
      response = LLM(messages, tools)
      if not has_tool_use(response): return
      results = execute_tools(response)
      messages.append(tool_results)

变化的只是循环周围的 harness：hooks、权限、压缩、记忆、后台、cron、团队、worktree、MCP
都挂在循环的不同位置。模型负责判断和行动选择；harness 负责组织环境。
"""
import json
import os
import time
import random
import threading
import subprocess
import asyncio
from dataclasses import dataclass, asdict, field
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool, StructuredTool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()
TASKS_DIR = WORKDIR / ".tasks"
MAILBOX_DIR = WORKDIR / ".mailboxes"
WORKTREES_DIR = WORKDIR / ".worktrees"
MEMORY_DIR = WORKDIR / ".memory"


# ═══════════ hooks 系统（s04）═══════════
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(hook_point: str, fn):
    HOOKS[hook_point].append(fn)


def trigger_hooks(hook_point: str, *args):
    """触发某个 hook 点。PreToolUse 可返回阻断信息。"""
    for fn in HOOKS[hook_point]:
        r = fn(*args)
        if r:
            return r
    return None


# ═══════════ 权限（s03，挂在 PreToolUse）═══════════
DANGEROUS_COMMANDS = ["rm -rf", "mkfs", "shutdown", ":(){:|:&};:"]


def permission_check(tc) -> str:
    """危险命令拦截。返回非空 = 阻断。"""
    if tc.get("name") == "bash":
        cmd = tc.get("args", {}).get("command", "")
        for d in DANGEROUS_COMMANDS:
            if d in cmd:
                return f"拒绝：命令含危险操作 {d!r}"
    return ""


register_hook("PreToolUse", permission_check)


# ═══════════ 记忆（s09，简化：文件仓库 + 索引）═══════════
def build_memory_catalog() -> str:
    """把 MEMORY.md 索引注入 system。"""
    f = MEMORY_DIR / "MEMORY.md"
    if f.exists():
        return f.read_text(encoding="utf-8")[:2000]
    return "（无记忆）"


# ═══════════ 任务系统（s12/s17，简化）═══════════
@dataclass
class Task:
    id: str
    subject: str
    status: str = "pending"
    owner: str = ""
    blockedBy: list = field(default_factory=list)
    worktree: str = ""


def load_task(tid):
    return Task(**json.loads((TASKS_DIR / f"{tid}.json").read_text(encoding="utf-8")))


def save_task(t):
    TASKS_DIR.mkdir(exist_ok=True)
    (TASKS_DIR / f"{t.id}.json").write_text(json.dumps(asdict(t), indent=2), encoding="utf-8")


def can_start(tid):
    for dep in load_task(tid).blockedBy:
        if load_task(dep).status != "completed":
            return False
    return True


# ═══════════ 工具（核心集合，覆盖 s02-s19 机制）═══════════
@tool
def bash(command: str) -> str:
    """执行 shell 命令。慢操作（install/build/test）会自动放后台。"""
    import subprocess
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


@tool
def read_file(path: str) -> str:
    """读文件。"""
    p = Path(path)
    return p.read_text(encoding="utf-8")[:2000] if p.exists() else f"不存在：{path}"


@tool
def write_file(path: str, content: str) -> str:
    """写文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path}"


@tool
def todo_write(todos: str) -> str:
    """写计划（会话内轻量计划）。todos 是 JSON 字符串。"""
    return f"计划已记录：{todos}"


@tool
def load_skill(name: str) -> str:
    """加载技能（按需注入技能正文）。"""
    p = WORKDIR / "skills" / name / "SKILL.md"
    return p.read_text(encoding="utf-8")[:2000] if p.exists() else f"技能 {name} 不存在"


@tool
def compact() -> str:
    """手动压缩上下文（保留最近 5 条 + 摘要）。"""
    return "上下文已压缩"


@tool
def create_task(subject: str) -> str:
    """创建任务。"""
    tid = f"task_{random.randint(0, 999999):06d}"
    save_task(Task(id=tid, subject=subject))
    return f"Created {tid}: {subject}"


@tool
def list_tasks() -> str:
    """列出任务。"""
    if not TASKS_DIR.exists():
        return "（无任务）"
    lines = [f"{t['id']} [{t['status']}] {t['subject']} owner={t.get('owner','')}"
             for f in sorted(TASKS_DIR.glob("task_*.json"))
             for t in [json.loads(f.read_text(encoding="utf-8"))]]
    return "\n".join(lines) if lines else "（无任务）"


@tool
def claim_task(task_id: str) -> str:
    """认领任务。"""
    t = load_task(task_id)
    if t.status != "pending":
        return f"Task {task_id} is {t.status}"
    t.owner = "agent"
    t.status = "in_progress"
    save_task(t)
    return f"Claimed {task_id}"


@tool
def complete_task(task_id: str) -> str:
    """完成任务。"""
    t = load_task(task_id)
    t.status = "completed"
    save_task(t)
    return f"Completed {task_id}"


@tool
def schedule_cron(cron: str, prompt: str) -> str:
    """注册定时任务（cron 五段式）。"""
    return f"已注册定时任务（{cron}）：{prompt}"


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动队友线程。"""
    return f"已启动队友 {name}（{role}）"


@tool
def send_message(to_agent: str, content: str) -> str:
    """发消息给其他 agent。"""
    return f"已发送给 {to_agent}"


@tool
def request_shutdown(name: str) -> str:
    """请求队友关机（握手）。"""
    return f"已请求 {name} 关机"


@tool
def review_plan(plan: str, approve: bool) -> str:
    """审批计划。"""
    return f"已{'批准' if approve else '拒绝'}计划"


@tool
def create_worktree(name: str) -> str:
    """创建隔离 worktree。"""
    import subprocess
    path = WORKTREES_DIR / name
    r = subprocess.run(["git", "worktree", "add", "-b", f"wt/{name}", str(path), "main"],
                       cwd=WORKDIR, capture_output=True, text=True)
    return f"已创建 worktree {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def remove_worktree(name: str) -> str:
    """删除 worktree。"""
    import subprocess
    r = subprocess.run(["git", "worktree", "remove", str(WORKTREES_DIR / name)],
                       cwd=WORKDIR, capture_output=True, text=True)
    return f"已删除 {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def connect_mcp(name: str) -> str:
    """连接 MCP server（当前仅 filesystem 可用）。"""
    return f"已连接 MCP server '{name}'"


BUILTIN_TOOLS = [bash, read_file, write_file, todo_write, load_skill, compact,
                 create_task, list_tasks, claim_task, complete_task,
                 schedule_cron, spawn_teammate, send_message, request_shutdown,
                 review_plan, create_worktree, remove_worktree, connect_mcp]
TOOL_HANDLERS = {t.name: t for t in BUILTIN_TOOLS}


def assemble_tool_pool():
    """组装工具池（内置 + MCP，s19）。"""
    tools = list(BUILTIN_TOOLS)
    handlers = dict(TOOL_HANDLERS)
    # MCP 工具（简化：演示架构，不真实连接）
    for server in mcp_servers:
        for mcp_tool in mcp_servers[server]:
            prefixed = f"mcp__{server}__{mcp_tool}"

            def _make_handler(s=server, t=mcp_tool):
                def handler(**kwargs):
                    return f"[MCP {s}.{t}] {kwargs}"
                return handler

            handlers[prefixed] = StructuredTool.from_function(
                func=_make_handler(),
                name=prefixed, description=f"MCP 工具 {server}.{mcp_tool}")
            tools.append(handlers[prefixed])
    return tools, handlers


mcp_servers: dict = {}   # server_name -> [tool names]


# ═══════════ system prompt 组装（s10）═══════════
def assemble_system_prompt() -> str:
    memory = build_memory_catalog()
    mcp_desc = ", ".join(mcp_servers) if mcp_servers else "无"
    return (f"你是 Agent 工程助手。\n"
            f"工作目录：{WORKDIR}\n"
            f"已连接 MCP server：{mcp_desc}\n"
            f"记忆：\n{memory}")


# ═══════════ 错误恢复（s11，简化）═══════════
def llm_with_retry(system, messages, max_retries=3):
    import time
    for i in range(max_retries):
        try:
            return llm.bind_tools(BUILTIN_TOOLS).invoke([SystemMessage(content=system)] + messages)
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                time.sleep(2 ** i)     # 指数退避
                continue
            raise
    raise RuntimeError(f"重试 {max_retries} 次仍失败")


# ═══════════ 统一循环（核心：机制很多，循环一个）═══════════
def agent_turn(query: str) -> str:
    """完整的一轮 Agent turn：所有机制挂在这一个 while True 上。"""
    trigger_hooks("UserPromptSubmit", query)          # ① 用户输入 hook

    messages = [HumanMessage(content=query)]
    tools, handlers = assemble_tool_pool()            # ② 组装工具池

    while True:
        system = assemble_system_prompt()             # ③ LLM 前：组装 system prompt

        response = llm_with_retry(system, messages)   # ④ LLM 调用（带错误恢复）
        messages.append(response)

        if not response.tool_calls:                   # ⑤ 无 tool_use = 结束
            trigger_hooks("Stop", response.content)
            return response.content

        results = []
        for tc in response.tool_calls:                # ⑥ 执行工具
            blocked = trigger_hooks("PreToolUse", tc)  # PreToolUse hook + 权限
            if blocked:
                results.append(ToolMessage(content=str(blocked), tool_call_id=tc["id"]))
                continue
            handler = handlers.get(tc["name"])
            if not handler:
                results.append(ToolMessage(
                    content=f"未知工具 {tc['name']}", tool_call_id=tc["id"]))
                continue
            try:
                result = handler.invoke(tc["args"])   # ⑦ 工具分发
            except Exception as e:
                result = f"工具执行出错：{e}"
            trigger_hooks("PostToolUse", tc, result)  # ⑧ PostToolUse hook
            results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        messages.extend(results)                      # ⑨ tool_result 回 messages，下一轮


if __name__ == "__main__":
    print("最终答案：\n")
    print(agent_turn("List the files in this directory."))
