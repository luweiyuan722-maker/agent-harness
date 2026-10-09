"""s17: Autonomous Agents —— 自己看板，自己认领

课程：learn-claudecode s17
主旨：「空闲时轮询，有活就干」—— 队友自组织，不依赖 Lead 分配。

三阶段生命周期：
  WORK（inbox→LLM→工具，最多 10 轮）→ IDLE（每 5s 轮询 inbox+任务板，60s 超时）→ SHUTDOWN

本章新增：
  scan_unclaimed_tasks（扫描可认领任务：pending + 无 owner + 依赖已完成）
  claim_task（认领 + owner 检查 + 锁防抢）
  idle_poll（空闲轮询：inbox 优先 + 任务板其次 + 超时）
"""
import json
import os
import time
import random
import threading
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


# ═══════════ 任务系统（复用 s12 核心）═══════════
@dataclass
class Task:
    id: str
    subject: str                       # 任务主题
    description: str = ""
    status: str = "pending"            # pending | in_progress | completed
    owner: str = ""                    # 认领者（空 = 未认领）
    blockedBy: list = field(default_factory=list)   # 依赖的任务 id 列表


def load_task(task_id: str) -> Task:
    f = TASKS_DIR / f"{task_id}.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    return Task(**data)


def save_task(task: Task) -> None:
    TASKS_DIR.mkdir(exist_ok=True)
    f = TASKS_DIR / f"{task.id}.json"
    f.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")


def can_start(task_id: str) -> bool:
    """所有 blockedBy 依赖都 completed 才能开始。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        dep = load_task(dep_id)
        if dep.status != "completed":
            return False
    return True


# ═══════════ MessageBus（复用 s16，带 metadata + 锁）═══════════
class MessageBus:
    """文件收件箱：.jsonl 邮箱，append 发消息，read+unlink 消费式读。"""

    def __init__(self):
        self._lock = threading.Lock()

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None) -> None:
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "metadata": metadata or {}, "ts": time.time()}
        MAILBOX_DIR.mkdir(exist_ok=True)
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with self._lock:
            with open(inbox, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with self._lock:
            if not inbox.exists():
                return []
            msgs = [json.loads(line) for line in inbox.read_text().splitlines()]
            inbox.unlink()
            return msgs


BUS = MessageBus()
_current = threading.local()


# ═══════════ 模块 1：scan_unclaimed_tasks（扫描可认领任务）═══════════
def scan_unclaimed_tasks() -> list[dict]:
    """扫描任务板上可认领的任务：pending + 无 owner + 依赖已完成。"""
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):   # sorted：认领顺序稳定
        task = json.loads(f.read_text(encoding="utf-8"))
        if (task.get("status") == "pending"           # ① 还没人做
                and not task.get("owner")             # ② 没人认领
                and can_start(task["id"])):           # ③ 依赖已完成
            unclaimed.append(task)
    return unclaimed


# ═══════════ 模块 2：claim_task（认领 + owner 检查 + 每任务一把锁）═══════════
_task_locks: dict[str, threading.Lock] = {}    # task_id → 专属锁
_locks_guard = threading.Lock()                # 保护 _task_locks 字典本身的锁


def _get_task_lock(task_id: str) -> threading.Lock:
    """获取某任务专属的锁（不存在就创建）。"""
    with _locks_guard:                          # ① 先锁字典本身
        if task_id not in _task_locks:
            _task_locks[task_id] = threading.Lock()
        return _task_locks[task_id]


def claim_task(task_id: str, owner: str) -> str:
    """认领任务：每任务一把锁，锁内完成 读-检查-改-写（原子，防并发抢）。"""
    with _get_task_lock(task_id):               # ② 只锁这个任务，其他任务不受影响
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} already owned by {task.owner}"
        if not can_start(task_id):
            return f"Blocked by dependencies"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        return f"Claimed {task.id} ({task.subject})"

# ═══════════ 模块 3：idle_poll（空闲轮询）═══════════
IDLE_POLL_INTERVAL = 5    # 秒
IDLE_TIMEOUT = 60         # 秒

def idle_poll(agent_name: str, messages: list, name: str, role: str) -> str:
    """空闲轮询：inbox 优先 + 任务板其次，返回 'work'/'shutdown'/'timeout'。"""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):   # 12 次 × 5 秒 = 60 秒
        time.sleep(IDLE_POLL_INTERVAL)                    # 每 5 秒醒来一次

        # ① inbox 优先（可能有 shutdown_request 等协议消息）
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(agent_name, "lead", "已收尾，关机", "shutdown_response",
                             {"request_id": msg.get("metadata", {}).get("request_id", ""),
                              "approve": True})
                    return "shutdown"
            messages.append(HumanMessage(
                content=f"[Inbox] {json.dumps(inbox)}"))
            return "work"

        # ② 任务板其次：扫到可认领的任务就 claim
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]                           # 排序后取第一个
            result = claim_task(task["id"], agent_name)
            if "Claimed" in result:                       # 认领成功才干活
                messages.append(HumanMessage(
                    content=f"认领了任务 {task['id']}：{task['subject']}。"
                            f"去完成它，完成后必须调用 complete_task 工具，"
                            f"参数 task_id 填 {task['id']}。"))
                return "work"

    return "timeout"

# ═══════════ 工具 ═══════════
@tool
def run_bash(command: str) -> str:
    """执行 shell 命令。"""
    import subprocess
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


@tool
def read_file(path: str) -> str:
    """读文件内容。"""
    p = Path(path)
    if not p.exists():
        return f"文件不存在：{path}"
    return p.read_text(encoding="utf-8")[:2000]


@tool
def write_file(path: str, content: str) -> str:
    """写文件（覆盖）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字符）"


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
        lines.append(f"{t['id']} [{t['status']}] {t['subject']} owner={t.get('owner', '')}")
    return "\n".join(lines) if lines else "（没有任务）"


@tool
def complete_task(task_id: str) -> str:
    """标记任务为已完成。"""
    task = load_task(task_id)
    if not task.owner:                              # 没认领就补上 owner（防 LLM 绕过 claim）
        task.owner = getattr(_current, "name", "")
    task.status = "completed"
    save_task(task)
    return f"Completed {task_id}"


@tool
def create_task(subject: str, description: str = "") -> str:
    """创建任务（放进任务板）。"""
    task_id = f"task_{random.randint(0, 999999):06d}"
    task = Task(id=task_id, subject=subject, description=description)
    save_task(task)
    return f"Created {task_id}: {subject}"


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动一个队友线程。"""
    return spawn_teammate_thread(name, role, prompt)


# 队友工具集（干活 + 标记完成，认领是 idle_poll 自动做的，不暴露给 LLM）
sub_tools = [send_message, read_file, write_file, run_bash, complete_task, list_tasks]
sub_tool_map = {t.name: t for t in sub_tools}

# Lead 工具集（创建任务 + 派活）
lead_tools = [spawn_teammate, create_task, list_tasks]
lead_tool_map = {t.name: t for t in lead_tools}
lead_llm = llm.bind_tools(lead_tools)


# ═══════════ Lead 主循环 ═══════════
SYSTEM = ("你是团队 Lead。用 create_task 创建任务放进任务板，用 spawn_teammate 启动队友，"
          "队友会自己扫描任务板、自己认领、自己完成，结果会进你的收件箱。")


def lead_loop(query: str):
    _current.name = "lead"
    messages = [HumanMessage(content=query)]
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            inbox_text = "\n".join(
                f"From {m['from']} ({m['type']}): {m['content'][:200]}" for m in inbox)
            messages.append(HumanMessage(content=f"[Inbox]\n{inbox_text}"))

        response = lead_llm.invoke([SystemMessage(content=SYSTEM)] + messages)
        messages.append(response)
        if not response.tool_calls:
            print(f"\n      💬 {response.content}")
            break
        for tc in response.tool_calls:
            result = lead_tool_map[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


# ═══════════ 模块 4：三阶段外层循环 + 身份重注入 ═══════════
def _teammate_loop(name, role, prompt):
    _current.name = name              # 线程局部：标记"我是谁"（send_message 用）
    system = f"You are '{name}', a {role}. Use tools to complete tasks."
    messages = [HumanMessage(content=prompt)]
    sub_llm = llm.bind_tools(sub_tools)
    summaries = []                    # 累积每个任务的 summary（不覆盖）

    # ═══ 外层 while True：WORK 和 IDLE 交替 ═══
    while True:
        # ── WORK 阶段（内层 for 10：最多 10 轮 LLM）──
        for _ in range(10):
            # ① 读收件箱，处理协议消息（shutdown_request → 回复并退出）
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(name, "lead", "已收尾，关机", "shutdown_response",
                             {"request_id": msg.get("metadata", {}).get("request_id", ""),
                              "approve": True})
                    _send_final_summary(name, summaries)
                    return                     # 收到关机，整个队友退出

            # ② 调 LLM
            sub_response = sub_llm.invoke([SystemMessage(content=system)] + messages)
            messages.append(sub_response)

            # ③ 不调工具 = 干完，break 出 WORK
            if not sub_response.tool_calls:
                summaries.append(sub_response.content)   # 累积本次任务的结果
                break
            # ④ 执行工具
            for tc in sub_response.tool_calls:
                print(f"      🔧 [{name}] {tc['name']}({tc['args']})")
                result = sub_tool_map[tc["name"]].invoke(tc["args"])
                messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        # ── IDLE 阶段（轮询找活）──
        result = idle_poll(name, messages, name, role)
        if result == "shutdown":
            _send_final_summary(name, summaries)
            break          # 收到关机 → 退出外层循环
        if result == "timeout":
            _send_final_summary(name, summaries)
            break          # 60 秒没活 → 超时退出
        # result == "work" → 不 break，回到 while 开头，再进 WORK 干活


def _send_final_summary(name: str, summaries: list) -> None:
    """把累积的所有任务结果发给 Lead（多个任务不覆盖）。"""
    if summaries:
        content = "\n".join(f"任务 {i+1}：{s}" for i, s in enumerate(summaries))
    else:
        content = "（未完成）"
    BUS.send(name, "lead", content, "result")
    print(f"      🤝 [{name}] 完成：{content[:80]}")


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """Lead 调用：启动一个队友 daemon 线程。"""
    threading.Thread(target=_teammate_loop, args=(name, role, prompt),
                     daemon=True).start()
    return f"已启动队友 {name}（{role}）：{prompt}"



if __name__ == "__main__":
    pass
