"""s16: Team Protocols —— 队友之间要有约定

课程：learn-claudecode s16
主旨：「request-response 模式驱动协商」—— 结构化握手。

三样新东西：
  ProtocolState（请求状态追踪：request_id + 状态机 pending→approved/rejected）
  dispatch_message（队友按消息类型路由到处理器）
  match_response（Lead 按 request_id 关联回复 + 类型校验）

两种协议，一套机制：
  shutdown_request/response：Lead→队友 关机握手
  plan_approval_request/response：队友→Lead 计划审批
"""
import json
import os
import time
import random
import threading
from dataclasses import dataclass
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


# ═══════════ MessageBus（承接 s15，加 metadata）═══════════
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


# ═══════════ 模块 1：ProtocolState + request_id ═══════════
@dataclass
class ProtocolState:
    request_id: str      # 唯一 ID，如 "req_004281"
    type: str            # "shutdown" | "plan_approval"
    sender: str          # 发起方
    target: str          # 接收方
    status: str          # pending | approved | rejected
    payload: str         # 计划文本或关机原因
    created_at: float    # 时间戳


pending_requests: dict[str, ProtocolState] = {}   # request_id → 状态
protocol_lock = threading.Lock()                  # 保护 pending_requests（多线程共享）


def new_request_id() -> str:
    """生成唯一 request_id。"""
    return f"req_{random.randint(0, 999999):06d}"


# ═══════════ 模块 2：match_response（关联 + 类型校验）═══════════
def match_response(response_type: str, request_id: str, approve: bool) -> None:
    """收到回复，按 request_id 找请求，校验类型，更新状态。"""
    state = pending_requests.get(request_id)
    if not state:
        return                                   # 校验①：没有这个请求
    if state.type == "shutdown" and response_type != "shutdown_response":
        return                                   # 校验②：类型不匹配（防串扰）
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        return
    if state.status != "pending":
        return                                   # 校验③：已处理（防重复）
    state.status = "approved" if approve else "rejected"


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
def submit_plan(plan: str) -> str:
    """提交计划给 Lead 审批（队友用）。"""
    name = getattr(_current, "name", "teammate")
    req_id = new_request_id()
    with protocol_lock:
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="plan_approval", sender=name, target="lead",
            status="pending", payload=plan, created_at=time.time())
    BUS.send(name, "lead", plan, "plan_approval_request", {"request_id": req_id})
    return f"计划已提交（{req_id}），等待 Lead 审批"


# 队友工具集（+ submit_plan）
sub_tools = [send_message, read_file, write_file, run_bash, submit_plan]
sub_tool_map = {t.name: t for t in sub_tools}


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动一个队友线程。"""
    return spawn_teammate_thread(name, role, prompt)


@tool
def request_shutdown(name: str) -> str:
    """请求队友关机（握手：队友确认收尾后才退出）。"""
    req_id = new_request_id()
    with protocol_lock:
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown", sender="lead", target=name,
            status="pending", payload="shutdown", created_at=time.time())
    BUS.send("lead", name, "请收尾并关机", "shutdown_request", {"request_id": req_id})
    return f"已请求 {name} 关机（{req_id}）"


@tool
def review_plan(request_id: str, approve: bool) -> str:
    """审批队友提交的计划。approve=True 批准，False 拒绝。"""
    state = pending_requests.get(request_id)
    if not state:
        return f"没有这个请求 {request_id}"
    BUS.send("lead", state.sender, "approved" if approve else "rejected",
             "plan_approval_response", {"request_id": request_id, "approve": approve})
    return f"已{'批准' if approve else '拒绝'}计划 {request_id}"


lead_tools = [spawn_teammate, request_shutdown, review_plan]
lead_tool_map = {t.name: t for t in lead_tools}
lead_llm = llm.bind_tools(lead_tools)


# ═══════════ 模块 3：dispatch + 队友 idle loop ═══════════
def handle_inbox_message(name: str, msg: dict, messages: list) -> bool:
    """队友收到消息，按类型路由。返回 True 表示"应该退出"。"""
    msg_type = msg.get("type", "message")
    meta = msg.get("metadata", {})
    req_id = meta.get("request_id", "")

    if msg_type == "shutdown_request":
        # 关机握手：确认收尾，回复，退出
        BUS.send(name, "lead", "已收尾，关机", "shutdown_response",
                 {"request_id": req_id, "approve": True})
        return True                                  # 停止循环

    if msg_type == "plan_approval_response":
        approve = meta.get("approve", False)
        match_response("plan_approval_response", req_id, approve)
        messages.append(HumanMessage(
            content="[Plan approved，继续执行]" if approve else "[Plan rejected，修正后重新提交]"))

    return False                                     # 继续循环


def _teammate_loop(name: str, role: str, prompt: str):
    """队友：工作循环（最多 10 轮）→ 干完发 summary → idle 等待关机。"""
    _current.name = name
    system = f"You are '{name}', a {role}. Use tools to complete tasks."
    messages = [HumanMessage(content=prompt)]
    sub_llm = llm.bind_tools(sub_tools)

    summary = ""
    # ── 工作循环 ──
    for _ in range(10):
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            if handle_inbox_message(name, msg, messages):
                return                                 # 收到 shutdown，退出

        response = sub_llm.invoke([SystemMessage(content=system)] + messages)
        messages.append(response)
        if not response.tool_calls:
            summary = response.content
            break
        for tc in response.tool_calls:
            result = sub_tool_map[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    BUS.send(name, "lead", summary or "（未完成）", "result")
    print(f"      🤝 [{name}] 完成，进入 idle 等待关机")

    # ── idle loop：干完不退出，轮询收件箱等 shutdown ──
    while True:
        time.sleep(1)
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            if handle_inbox_message(name, msg, messages):
                print(f"      🔌 [{name}] 已关机")
                return


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    threading.Thread(target=_teammate_loop, args=(name, role, prompt),
                     daemon=True).start()
    return f"已启动队友 {name}（{role}）：{prompt}"


# ═══════════ 模块 4：consume_lead_inbox + Lead 主循环 ═══════════
def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """统一消费 Lead 收件箱：先路由协议消息更新状态，再返回剩余内容。"""
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


SYSTEM = ("你是团队 Lead。用 spawn_teammate 派活，用 request_shutdown 请求关机，"
          "用 review_plan 审批计划。队友结果和回复会进你的收件箱。")


def lead_loop(query: str):
    _current.name = "lead"
    messages = [HumanMessage(content=query)]
    while True:
        inbox = consume_lead_inbox()
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
    lead_loop("Spawn alice as a backend developer, ask her to create config.py. "
              "Then request her shutdown.")
