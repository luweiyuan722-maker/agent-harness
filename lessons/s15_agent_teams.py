"""s15: Agent Teams —— 一个搞不定，组队来

课程：learn-claudecode s15
主旨：「文件收件箱 + 队友线程」—— 多 Agent 协作，消息总线。

三样新东西：
  MessageBus（.jsonl 文件收件箱，append 发消息 + read/unlink 消费式读）
  spawn_teammate_thread（队友 daemon 线程 + 自己的 system/messages/简化工具集）
  inbox 注入（Lead 读收件箱，注入 history）

子 Agent（s06）→ 队友（s15）：
  一次性 → 多轮；只回传结论 → 异步收件箱随时通信；上下文隔离 → 消息共享。
"""
import json
import os
import time
import threading
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
MAILBOX_DIR = WORKDIR / ".mailboxes"


# ═══════════ 模块 1：MessageBus（文件收件箱，加锁版）═══════════
class MessageBus:
    """文件收件箱：每个 Agent 一个 .jsonl 文件，append 发消息，read+unlink 消费式读。

    加锁原因：多个队友线程可能同时给同一个 agent（如 lead）发消息，并发写同一文件。
    """

    def __init__(self):
        self._lock = threading.Lock()       # 收件箱全局锁

    def send(self, from_agent: str, to_agent: str,
             content: str, msg_type: str = "message") -> None:
        """发消息 = 往对方邮箱文件里 append 一行 JSON。"""
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time()}
        MAILBOX_DIR.mkdir(exist_ok=True)
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with self._lock:                    # 🔒 串行 append，防并发写冲突
            with open(inbox, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent: str) -> list[dict]:
        """读消息 = 读文件 + 删除（消费式：每条消息恰好被读走一次）。"""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with self._lock:                    # 🔒 read+unlink 原子，防重复读/丢消息
            if not inbox.exists():
                return []
            msgs = [json.loads(line) for line in inbox.read_text().splitlines()]
            inbox.unlink()
            return msgs


BUS = MessageBus()

# 线程局部：记录当前线程是哪个 agent（send_message 工具用它知道"谁在发"）
_current = threading.local()


# ═══════════ 工具定义 ═══════════
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
    from_agent = getattr(_current, "name", "lead")   # 线程局部：谁在发
    BUS.send(from_agent, to_agent, content, "message")
    return f"已发送给 {to_agent}"


# 队友的简化工具集（bash + 读写 + 发消息，省略任务/cron，聚焦通信）
sub_tools = [send_message, read_file, write_file, run_bash]
sub_tool_map = {t.name: t for t in sub_tools}


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动一个队友线程来处理任务。name=名字，role=角色，prompt=任务。"""
    return spawn_teammate_thread(name, role, prompt)


# Lead 的工具集（主要就是派活 + 收结果）
lead_tools = [spawn_teammate]
lead_tool_map = {t.name: t for t in lead_tools}
lead_llm = llm.bind_tools(lead_tools)


# ═══════════ 模块 2：spawn_teammate_thread ═══════════
def _teammate_loop(name: str, role: str, prompt: str):
    """队友的循环：自己的 system/messages/工具集，最多 10 轮，干完发 summary 给 Lead。"""
    _current.name = name                        # 线程局部：标记"我是谁"（send_message 用）
    system = f"You are '{name}', a {role}. Use tools to complete tasks."
    messages = [HumanMessage(content=prompt)]
    sub_llm = llm.bind_tools(sub_tools)

    summary = ""
    for _ in range(10):                         # 任务预算：最多 10 轮，防失控
        inbox = BUS.read_inbox(name)            # 读自己的收件箱
        if inbox:
            messages.append(HumanMessage(
                content=f"<inbox>{json.dumps(inbox)}</inbox>"))

        response = sub_llm.invoke([SystemMessage(content=system)] + messages)
        messages.append(response)

        if not response.tool_calls:             # 不调工具 = 干完
            summary = response.content
            break

        for tc in response.tool_calls:
            result = sub_tool_map[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    BUS.send(name, "lead", summary or "（未完成）", "result")   # 发 summary 给 Lead
    print(f"      🤝 [{name}] 完成：{summary[:80]}")


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """Lead 调用：启动一个队友 daemon 线程。"""
    threading.Thread(target=_teammate_loop, args=(name, role, prompt),
                     daemon=True).start()
    return f"已启动队友 {name}（{role}）：{prompt}"


# ═══════════ 模块 3：Lead 主循环 + inbox 注入 ═══════════
SYSTEM = ("你是团队 Lead。用 spawn_teammate 工具派活给队友（名字/角色/任务），"
          "队友完成后结果会进你的收件箱。")


def lead_loop(query: str):
    """Lead 循环：while True 常驻，每轮先读收件箱（队友结果注入），再调模型。"""
    messages = [HumanMessage(content=query)]
    while True:
        # inbox 注入：读收件箱，队友的结果作为 [Inbox] 消息注入
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


if __name__ == "__main__":
    lead_loop("Spawn alice as a backend developer. Ask her to create a file "
              "called schema.sql with a users table.")
