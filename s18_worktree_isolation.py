"""s18: Worktree Isolation —— 各干各的目录，互不干扰

课程：learn-claudecode s18
主旨：「任务管目标，worktree 管目录，按 ID 绑定」—— 并行执行的目录隔离。

本章新增：
  create_worktree（git worktree add 创建独立目录 + 分支）
  bind_task_to_worktree（任务绑定 worktree，不改状态）
  remove_worktree / keep_worktree（收尾：清理或保留）
  validate_worktree_name（拒绝路径穿越和非法字符）
  wt_ctx（队友认领带 worktree 的任务后，工具 cwd 自动切换）
"""
import json
import os
import time
import random
import threading
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()
TASKS_DIR = WORKDIR / ".tasks"
MAILBOX_DIR = WORKDIR / ".mailboxes"
WORKTREES_DIR = WORKDIR / ".worktrees"


# ═══════════ 任务系统（复用 s17，Task 加 worktree 字段）═══════════
@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str = ""
    blockedBy: list = field(default_factory=list)
    worktree: str = ""          # 绑定的 worktree 名字（空 = 未绑定）


def load_task(task_id: str) -> Task:
    f = TASKS_DIR / f"{task_id}.json"
    return Task(**json.loads(f.read_text(encoding="utf-8")))


def save_task(task: Task) -> None:
    TASKS_DIR.mkdir(exist_ok=True)
    (TASKS_DIR / f"{task.id}.json").write_text(
        json.dumps(asdict(task), indent=2), encoding="utf-8")


def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if load_task(dep_id).status != "completed":
            return False
    return True


# ═══════════ MessageBus（复用）═══════════
class MessageBus:
    def __init__(self):
        self._lock = threading.Lock()

    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        msg = {"from": from_agent, "to": to_agent, "content": content,
               "type": msg_type, "metadata": metadata or {}, "ts": time.time()}
        MAILBOX_DIR.mkdir(exist_ok=True)
        with self._lock:
            with open(MAILBOX_DIR / f"{to_agent}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent):
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with self._lock:
            if not inbox.exists():
                return []
            msgs = [json.loads(line) for line in inbox.read_text().splitlines()]
            inbox.unlink()
            return msgs


BUS = MessageBus()
_current = threading.local()


# ═══════════ 模块 1：run_git + validate_worktree_name ═══════════
def run_git(args: list) -> tuple:
    """执行 git 命令，返回 (成功?, 输出)。"""
    r = subprocess.run(["git"] + args, cwd=WORKDIR,
                       capture_output=True, text=True)
    out = (r.stdout or "").strip() + ((" " + r.stderr.strip()) if r.stderr.strip() else "")
    return r.returncode == 0, out


def validate_worktree_name(name: str) -> str:
    """校验 worktree 名字，非法返回错误信息，合法返回空字符串。"""
    if not name:
        return "名字不能为空"
    if name in (".", "..") or "/" in name or "\\" in name:
        return f"非法名字 {name!r}：不能含路径分隔符或点号"
    for ch in name:
        if not (ch.isalnum() or ch in "._-"):
            return f"非法名字 {name!r}：只允许字母数字和 . _ -"
    return ""


# ═══════════ 模块 2：create / bind / remove / keep ═══════════
def create_worktree(name: str) -> str:
    """创建独立 worktree（目录 + 分支）。"""
    err = validate_worktree_name(name)
    if err:
        return f"创建失败：{err}"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"创建失败：{name} 已存在"
    ok, out = run_git(["worktree", "add", "-b", f"wt/{name}", str(path), "main"])
    if not ok:
        return f"创建失败：{out}"
    log_event("create", name)
    return f"已创建 worktree {name}（分支 wt/{name}）"


def bind_task_to_worktree(task_id: str, worktree_name: str) -> str:
    """把任务绑定到 worktree（不改任务状态，任务仍是 pending）。"""
    task = load_task(task_id)
    if task.worktree:
        return f"任务 {task_id} 已绑定 {task.worktree}"
    if not (WORKTREES_DIR / worktree_name).exists():
        return f"绑定失败：worktree {worktree_name} 不存在"
    task.worktree = worktree_name
    save_task(task)
    return f"已绑定任务 {task_id} → worktree {worktree_name}（状态仍 pending）"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """删除 worktree。有未提交改动时默认拒绝。"""
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"{name} 不存在"
    args = ["worktree", "remove"]           # 条件参数用 append，不传空字符串
    if discard_changes:
        args.append("--force")
    args.append(str(path))
    ok, out = run_git(args)
    if not ok:
        return f"删除失败：{out}"
    log_event("remove", name)
    return f"已删除 worktree {name}"


def keep_worktree(name: str) -> str:
    """保留 worktree（等人工 review 后合并）。"""
    log_event("keep", name)
    return f"已保留 worktree {name}（等人工 review 合并）"


def log_event(event_type: str, worktree_name: str, task_id: str = "") -> None:
    """写生命周期事件到 .worktrees/events.jsonl（审计）。"""
    WORKTREES_DIR.mkdir(exist_ok=True)
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    with open(WORKTREES_DIR / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# ═══════════ 模块 3：scan + claim（复用 s17）═══════════
_task_locks: dict = {}
_locks_guard = threading.Lock()


def _get_task_lock(task_id):
    with _locks_guard:
        if task_id not in _task_locks:
            _task_locks[task_id] = threading.Lock()
        return _task_locks[task_id]


def scan_unclaimed_tasks():
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text(encoding="utf-8"))
        if (task.get("status") == "pending" and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id, owner):
    with _get_task_lock(task_id):
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} already owned by {task.owner}"
        if not can_start(task_id):
            return "Blocked by dependencies"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        return f"Claimed {task.id} ({task.subject})"


# ═══════════ 工具 ═══════════
@tool
def run_bash(command: str) -> str:
    """执行 shell 命令（在队友当前 worktree 目录下）。"""
    cwd = getattr(_current, "cwd", None)
    r = subprocess.run(command, shell=True, capture_output=True, text=True,
                       timeout=30, cwd=cwd)
    return (r.stdout or r.stderr or "(无输出)").strip()


@tool
def read_file(path: str) -> str:
    """读文件（相对路径在 worktree 下）。"""
    cwd = getattr(_current, "cwd", None)
    p = Path(path)
    if cwd and not p.is_absolute():
        p = Path(cwd) / p
    if not p.exists():
        return f"文件不存在：{path}"
    return p.read_text(encoding="utf-8")[:2000]


@tool
def write_file(path: str, content: str) -> str:
    """写文件（相对路径在 worktree 下）。"""
    cwd = getattr(_current, "cwd", None)
    p = Path(path)
    if cwd and not p.is_absolute():
        p = Path(cwd) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）"


@tool
def send_message(to_agent: str, content: str) -> str:
    """发消息给另一个 agent。"""
    from_agent = getattr(_current, "name", "lead")
    BUS.send(from_agent, to_agent, content, "message")
    return f"已发送给 {to_agent}"


@tool
def list_tasks() -> str:
    """列出任务板上所有任务。"""
    if not TASKS_DIR.exists():
        return "（没有任务）"
    lines = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        t = json.loads(f.read_text(encoding="utf-8"))
        lines.append(f"{t['id']} [{t['status']}] {t['subject']} "
                     f"owner={t.get('owner','')} worktree={t.get('worktree','')}")
    return "\n".join(lines) if lines else "（没有任务）"


@tool
def complete_task(task_id: str) -> str:
    """标记任务为已完成。"""
    task = load_task(task_id)
    if not task.owner:
        task.owner = getattr(_current, "name", "")
    task.status = "completed"
    save_task(task)
    return f"Completed {task_id}"


@tool
def create_task(subject: str, description: str = "") -> str:
    """创建任务（放进任务板）。"""
    task_id = f"task_{random.randint(0, 999999):06d}"
    save_task(Task(id=task_id, subject=subject, description=description))
    return f"Created {task_id}: {subject}"


@tool
def create_worktree_tool(name: str) -> str:
    """为任务创建独立 worktree。"""
    return create_worktree(name)


@tool
def bind_task(name: str, worktree_name: str) -> str:
    """把任务绑定到 worktree。name 是 task_id。"""
    return bind_task_to_worktree(name, worktree_name)


@tool
def remove_worktree_tool(name: str, discard_changes: bool = False) -> str:
    """删除 worktree。"""
    return remove_worktree(name, discard_changes)


@tool
def keep_worktree_tool(name: str) -> str:
    """保留 worktree。"""
    return keep_worktree(name)


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动一个队友线程。"""
    return spawn_teammate_thread(name, role, prompt)


sub_tools = [send_message, read_file, write_file, run_bash, complete_task, list_tasks]
sub_tool_map = {t.name: t for t in sub_tools}

lead_tools = [spawn_teammate, create_task, list_tasks,
              create_worktree_tool, bind_task, remove_worktree_tool, keep_worktree_tool]
lead_tool_map = {t.name: t for t in lead_tools}
lead_llm = llm.bind_tools(lead_tools)


# ═══════════ 队友循环（认领后切换 cwd）═══════════
IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def idle_poll(agent_name, messages, name, role):
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(agent_name, "lead", "已收尾，关机", "shutdown_response",
                             {"request_id": msg.get("metadata", {}).get("request_id", ""),
                              "approve": True})
                    return "shutdown"
            messages.append(HumanMessage(content=f"[Inbox] {json.dumps(inbox)}"))
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], agent_name)
            if "Claimed" in result:
                # 认领成功：如果任务绑定了 worktree，切换 cwd
                t = load_task(task["id"])
                if t.worktree:
                    _current.cwd = str(WORKTREES_DIR / t.worktree)
                messages.append(HumanMessage(
                    content=f"认领了任务 {task['id']}：{task['subject']}。"
                            f"完成后必须调用 complete_task 工具，参数 task_id 填 {task['id']}。"))
                return "work"
    return "timeout"


def _teammate_loop(name, role, prompt):
    _current.name = name
    system = f"You are '{name}', a {role}. Use tools to complete tasks."
    messages = [HumanMessage(content=prompt)]
    sub_llm = llm.bind_tools(sub_tools)
    summaries = []

    while True:
        for _ in range(10):
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(name, "lead", "已收尾，关机", "shutdown_response",
                             {"request_id": msg.get("metadata", {}).get("request_id", ""),
                              "approve": True})
                    _send_final_summary(name, summaries)
                    return
            sub_response = sub_llm.invoke([SystemMessage(content=system)] + messages)
            messages.append(sub_response)
            if not sub_response.tool_calls:
                summaries.append(sub_response.content)
                break
            for tc in sub_response.tool_calls:
                result = sub_tool_map[tc["name"]].invoke(tc["args"])
                messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        result = idle_poll(name, messages, name, role)
        if result in ("shutdown", "timeout"):
            _send_final_summary(name, summaries)
            break


def _send_final_summary(name, summaries):
    content = "\n".join(f"任务 {i+1}：{s}" for i, s in enumerate(summaries)) if summaries else "（未完成）"
    BUS.send(name, "lead", content, "result")
    print(f"      🤝 [{name}] 完成：{content[:80]}")


def spawn_teammate_thread(name, role, prompt):
    threading.Thread(target=_teammate_loop, args=(name, role, prompt), daemon=True).start()
    return f"已启动队友 {name}（{role}）"


# ═══════════ Lead 主循环 ═══════════
SYSTEM = ("你是团队 Lead。用 create_task 建任务、create_worktree_tool 建 worktree、"
          "bind_task 绑定任务和 worktree、spawn_teammate 启动队友。")


def lead_loop(query):
    _current.name = "lead"
    messages = [HumanMessage(content=query)]
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            inbox_text = "\n".join(f"From {m['from']} ({m['type']}): {m['content'][:200]}" for m in inbox)
            messages.append(HumanMessage(content=f"[Inbox]\n{inbox_text}"))
        response = lead_llm.invoke([SystemMessage(content=SYSTEM)] + messages)
        messages.append(response)
        if not response.tool_calls:
            print(f"\n      💬 {response.content}")
            break
        for tc in response.tool_calls:
            result = lead_tool_map[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


if __name__ == "__main__":
    pass
