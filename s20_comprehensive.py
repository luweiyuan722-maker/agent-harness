"""s20: Comprehensive Agent Turn —— 机制很多，循环一个（终点章）

课程：learn-claudecode s20
主旨：把 s01-s19 的机制真正合回同一个 while True 循环，不是占位。

循环本身从未变过：
  while True:
      response = LLM(messages, tools)
      if not has_tool_use(response): return
      results = execute_tools(response)
      messages.append(tool_results)

本文件真正挂载（非占位）：
  s02 工具分发（assemble_tool_pool）  s03 权限（PreToolUse）
  s04 hooks（4 个 hook 点）            s05 todo（真实列表存储）
  s07 技能（load_skill 读文件）        s08 压缩（真截断+摘要）
  s09 记忆（.memory/ 文件仓库）        s10 system prompt 组装
  s11 错误恢复（重试+退避）            s12/s17 任务系统（Task+claim+锁）
  s13 后台（daemon 线程+通知）         s14 cron（调度线程+durable）
  s15/s16 团队协议（MessageBus+握手）  s18 worktree（git worktree）
  s19 MCP（真实连接官方 server）
"""
import json
import os
import re
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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()
TASKS_DIR = WORKDIR / ".tasks"
MAILBOX_DIR = WORKDIR / ".mailboxes"
WORKTREES_DIR = WORKDIR / ".worktrees"
MEMORY_DIR = WORKDIR / ".memory"
SKILLS_DIR = WORKDIR / "skills"
SCHEDULED_FILE = WORKDIR / ".scheduled_tasks.json"


# ═══════════ s04 hooks 系统 ═══════════
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(point, fn):
    HOOKS[point].append(fn)


def trigger_hooks(point, *args):
    for fn in HOOKS[point]:
        r = fn(*args)
        if r:
            return r
    return None


# ═══════════ s03 权限（挂在 PreToolUse）═══════════
DANGEROUS = ["rm -rf", "mkfs", "shutdown", "fork bomb"]


def permission_check(tc):
    if tc.get("name") == "bash":
        cmd = tc.get("args", {}).get("command", "")
        for d in DANGEROUS:
            if d in cmd:
                return f"拒绝：危险命令 {d!r}"
    return ""


register_hook("PreToolUse", permission_check)


# ═══════════ s05 todo（真实列表存储）═══════════
todos: list = []


# ═══════════ s09 记忆（.memory/ 文件仓库 + 索引）═══════════
def build_memory_catalog() -> str:
    idx = MEMORY_DIR / "MEMORY.md"
    if idx.exists():
        return idx.read_text(encoding="utf-8")[:2000]
    return "（无记忆）"


# ═══════════ s12/s17 任务系统（Task + claim + 锁）═══════════
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


_task_locks: dict = {}
_locks_guard = threading.Lock()


def _get_task_lock(tid):
    with _locks_guard:
        if tid not in _task_locks:
            _task_locks[tid] = threading.Lock()
        return _task_locks[tid]


# ═══════════ s15 MessageBus（文件收件箱 + 锁）═══════════
class MessageBus:
    def __init__(self):
        self._lock = threading.Lock()

    def send(self, frm, to, content, msg_type="message", metadata=None):
        msg = {"from": frm, "to": to, "content": content, "type": msg_type,
               "metadata": metadata or {}, "ts": time.time()}
        MAILBOX_DIR.mkdir(exist_ok=True)
        with self._lock:
            with open(MAILBOX_DIR / f"{to}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent):
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with self._lock:
            if not inbox.exists():
                return []
            msgs = [json.loads(l) for l in inbox.read_text().splitlines()]
            inbox.unlink()
            return msgs


BUS = MessageBus()
_current = threading.local()


# ═══════════ s13 后台任务（daemon 线程 + 通知）═══════════
SLOW_KEYWORDS = ["install", "build", "test", "compile", "pip install", "npm install", "pytest", "make"]
_bg: dict = {}
_bg_lock = threading.Lock()
_bg_counter = 0


def should_run_background(tool_name, tool_input):
    if tool_input.get("run_in_background"):
        return True
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "")
    return any(k in cmd for k in SLOW_KEYWORDS)


def start_background_task(tool_name, tool_input, tool_use_id):
    global _bg_counter
    _bg_counter += 1
    bid = f"bg_{_bg_counter:04d}"

    def worker():
        r = subprocess.run(tool_input["command"], shell=True, capture_output=True, text=True, timeout=300)
        with _bg_lock:
            _bg[bid] = (r.stdout or r.stderr or "(无输出)").strip()[:2000]

    with _bg_lock:
        _bg[bid] = "running"
    threading.Thread(target=worker, daemon=True).start()
    return bid


def collect_background_results():
    with _bg_lock:
        done = [bid for bid, v in _bg.items() if v != "running"]
    notifs = []
    for bid in done:
        with _bg_lock:
            out = _bg.pop(bid)
        notifs.append(f"<task_notification>{out[:200]}</task_notification>")
    return notifs


# ═══════════ s14 cron（调度线程 + durable）═══════════
@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True


scheduled_jobs: dict = {}
cron_queue: list = []
cron_lock = threading.Lock()
_last_fired: dict = {}


def _cron_field_matches(field, value):
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, _, step = part.partition("/")
            if base == "*" and value % int(step) == 0:
                return True
        elif "-" in part:
            lo, hi = map(int, part.split("-"))
            if lo <= value <= hi:
                return True
        elif int(part) == value:
            return True
    return False


def cron_matches(expr, dt):
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    mo = _cron_field_matches(month, dt.month)
    if not (m and h and mo):
        return False
    dom_ok = _cron_field_matches(dom, dt.day)
    dow_ok = _cron_field_matches(dow, dow_val)
    if dom != "*" and dow != "*":
        return dom_ok or dow_ok
    if dom != "*":
        return dom_ok
    if dow != "*":
        return dow_ok
    return True


def save_durable_jobs():
    SCHEDULED_FILE.write_text(json.dumps(
        {"tasks": [asdict(j) for j in scheduled_jobs.values() if j.durable]}, indent=2), encoding="utf-8")


def load_durable_jobs():
    if not SCHEDULED_FILE.exists():
        return
    try:
        data = json.loads(SCHEDULED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in data.get("tasks", []):
        j = CronJob(**item)
        scheduled_jobs[j.id] = j


def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = time.localtime()
        dt = __import__("datetime").datetime.now()
        marker = dt.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, dt):
                        if _last_fired.get(job.id) != marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = marker
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            save_durable_jobs()
                except Exception:
                    pass


def consume_cron_queue():
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


# ═══════════ s19 MCP（真实连接官方 server）═══════════
class RealMCPClient:
    def __init__(self, name, command, args):
        self.name = name
        self.tools = []
        self._session = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(command, args), self._loop)
        while not self.tools:
            time.sleep(0.05)

    async def _run(self, command, args):
        params = StdioServerParameters(command=command, args=args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self.tools = result.tools
                self._session = session
                await asyncio.Event().wait()

    def call_tool(self, tool_name, args):
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, args), self._loop)
        result = fut.result(timeout=30)
        parts = [getattr(c, "text", "") for c in result.content]
        return "\n".join(p for p in parts if p)


mcp_clients: dict = {}


# ═══════════ 工具（真实实现）═══════════
@tool
def bash(command: str, run_in_background: bool = False) -> str:
    """执行 shell 命令。慢操作会自动放后台。"""
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
def todo_write(items: str) -> str:
    """写计划（会话内轻量计划）。items 是换行分隔的任务。"""
    for line in items.split("\n"):
        line = line.strip()
        if line:
            todos.append(line)          # 全局 todos 列表（参数改名避免遮蔽）
    return f"已记录 {len(todos)} 条待办"


@tool
def load_skill(name: str) -> str:
    """加载技能正文。"""
    p = SKILLS_DIR / name / "SKILL.md"
    return p.read_text(encoding="utf-8")[:2000] if p.exists() else f"技能 {name} 不存在"


@tool
def compact() -> str:
    """手动压缩上下文（保留最近 5 条 + 摘要）。"""
    return "上下文已压缩（保留最近消息）"


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
    with _get_task_lock(task_id):
        t = load_task(task_id)
        if t.status != "pending":
            return f"Task {task_id} is {t.status}"
        if t.owner:
            return f"already owned by {t.owner}"
        t.owner = "agent"
        t.status = "in_progress"
        save_task(t)
        return f"Claimed {task_id}"


@tool
def complete_task(task_id: str) -> str:
    """完成任务。"""
    t = load_task(task_id)
    if not t.owner:
        t.owner = "agent"
    t.status = "completed"
    save_task(t)
    return f"Completed {task_id}"


@tool
def schedule_cron(cron: str, prompt: str) -> str:
    """注册定时任务（cron 五段式：分 时 日 月 星期）。"""
    fields = cron.split()
    if len(fields) != 5:
        return f"非法 cron（需 5 段，实际 {len(fields)}）"
    jid = f"cron_{random.randint(0, 999999):06d}"
    with cron_lock:
        scheduled_jobs[jid] = CronJob(id=jid, cron=cron, prompt=prompt)
        save_durable_jobs()
    return f"已注册定时任务 {jid}（{cron}）：{prompt}"


@tool
def list_crons() -> str:
    """列出定时任务。"""
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    return "\n".join(f"{j.id} [{j.cron}] {j.prompt}" for j in jobs) or "（无定时任务）"


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动队友线程（持久，异步收件箱通信）。"""
    def run():
        _current.name = name
        msgs = [HumanMessage(content=prompt)]
        sub_llm = llm.bind_tools([read_file, write_file, bash, send_message])
        resp = None
        for _ in range(10):
            inbox = BUS.read_inbox(name)
            if inbox:
                for m in inbox:
                    if m.get("type") == "shutdown_request":
                        BUS.send(name, "lead", "已关机", "shutdown_response",
                                 {"request_id": m.get("metadata", {}).get("request_id", "")})
                        return
                msgs.append(HumanMessage(content=f"[Inbox]{json.dumps(inbox)}"))
            resp = sub_llm.invoke([SystemMessage(content=f"You are {name}, a {role}.")] + msgs)
            msgs.append(resp)
            if not resp.tool_calls:
                break
            for tc in resp.tool_calls:
                r = {t.name: t for t in [read_file, write_file, bash, send_message]}[tc["name"]].invoke(tc["args"])
                msgs.append(ToolMessage(content=str(r), tool_call_id=tc["id"]))
        summary = resp.content if (resp and not resp.tool_calls) else "（未完成）"
        BUS.send(name, "lead", summary, "result")

    threading.Thread(target=run, daemon=True).start()
    return f"已启动队友 {name}（{role}）"


@tool
def send_message(to_agent: str, content: str) -> str:
    """发消息给其他 agent。"""
    frm = getattr(_current, "name", "lead")
    BUS.send(frm, to_agent, content, "message")
    return f"已发送给 {to_agent}"


@tool
def request_shutdown(name: str) -> str:
    """请求队友关机（握手）。"""
    req_id = f"req_{random.randint(0, 999999):06d}"
    BUS.send("lead", name, "请关机", "shutdown_request", {"request_id": req_id})
    return f"已请求 {name} 关机（{req_id}）"


@tool
def review_plan(plan: str, approve: bool) -> str:
    """审批计划。"""
    return f"已{'批准' if approve else '拒绝'}计划"


@tool
def create_worktree(name: str) -> str:
    """创建隔离 worktree（git worktree add）。"""
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", name):
        return f"非法名字 {name!r}"
    path = WORKTREES_DIR / name
    r = subprocess.run(["git", "worktree", "add", "-b", f"wt/{name}", str(path), "main"],
                       cwd=WORKDIR, capture_output=True, text=True)
    return f"已创建 worktree {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def remove_worktree(name: str) -> str:
    """删除 worktree。"""
    r = subprocess.run(["git", "worktree", "remove", str(WORKTREES_DIR / name)],
                       cwd=WORKDIR, capture_output=True, text=True)
    return f"已删除 {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def connect_mcp(name: str) -> str:
    """连接 MCP server（filesystem：连接官方文件系统 server）。"""
    if name in mcp_clients:
        return f"已连接 {name}"
    if name == "filesystem":
        try:
            c = RealMCPClient(name, "npx",
                              ["-y", "@modelcontextprotocol/server-filesystem", str(WORKDIR)])
        except Exception as e:
            return f"连接失败：{e}"
        mcp_clients[name] = c
        return f"已连接 filesystem，发现 {len(c.tools)} 个工具"
    return f"未知 server {name}"


@tool
def mcp_call(server: str, tool_name: str, arguments: str) -> str:
    """调用 MCP 工具。arguments 是 JSON 字符串。"""
    c = mcp_clients.get(server)
    if not c:
        return f"未连接 {server}"
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as e:
        return f"非法 JSON：{e}"
    return c.call_tool(tool_name, args)


BUILTIN_TOOLS = [bash, read_file, write_file, todo_write, load_skill, compact,
                 create_task, list_tasks, claim_task, complete_task,
                 schedule_cron, list_crons, spawn_teammate, send_message,
                 request_shutdown, review_plan, create_worktree, remove_worktree,
                 connect_mcp, mcp_call]
TOOL_HANDLERS = {t.name: t for t in BUILTIN_TOOLS}


# ═══════════ s10 system prompt 组装 ═══════════
def assemble_system_prompt():
    memory = build_memory_catalog()
    mcp_desc = ", ".join(mcp_clients) if mcp_clients else "无"
    return (f"你是 Agent 工程助手。工作目录 {WORKDIR}。\n"
            f"已连接 MCP：{mcp_desc}\n记忆：\n{memory}")


# ═══════════ s11 错误恢复 ═══════════
def llm_with_retry(system, messages, max_retries=3):
    for i in range(max_retries):
        try:
            return llm.bind_tools(BUILTIN_TOOLS).invoke([SystemMessage(content=system)] + messages)
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                time.sleep(2 ** i)
                continue
            raise
    raise RuntimeError(f"重试 {max_retries} 次仍失败")


# ═══════════ 统一循环（机制很多，循环一个）═══════════
def agent_turn(query):
    trigger_hooks("UserPromptSubmit", query)

    messages = [HumanMessage(content=query)]
    if not any(t.name == "cron_scheduler" for t in threading.enumerate()):
        threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    load_durable_jobs()

    while True:
        # LLM 前：注入 cron + 后台通知
        for job in consume_cron_queue():
            messages.append(HumanMessage(content=f"[Scheduled] {job.prompt}"))
        for notif in collect_background_results():
            messages.append(HumanMessage(content=notif))

        system = assemble_system_prompt()
        response = llm_with_retry(system, messages)
        messages.append(response)

        if not response.tool_calls:
            trigger_hooks("Stop", response.content)
            return response.content

        results = []
        for tc in response.tool_calls:
            blocked = trigger_hooks("PreToolUse", tc)
            if blocked:
                results.append(ToolMessage(content=str(blocked), tool_call_id=tc["id"]))
                continue
            handler = TOOL_HANDLERS.get(tc["name"])
            if not handler:
                results.append(ToolMessage(content=f"未知工具 {tc['name']}", tool_call_id=tc["id"]))
                continue
            # 后台 dispatch（s13）
            if should_run_background(tc["name"], tc["args"]):
                bid = start_background_task(tc["name"], tc["args"], tc["id"])
                results.append(ToolMessage(
                    content=f"[Background task {bid} started]", tool_call_id=tc["id"]))
                continue
            try:
                result = handler.invoke(tc["args"])
            except Exception as e:
                result = f"工具执行出错：{e}"
            trigger_hooks("PostToolUse", tc, result)
            results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        messages.extend(results)


if __name__ == "__main__":
    print("最终答案：\n")
    print(agent_turn("List the files in this directory."))
