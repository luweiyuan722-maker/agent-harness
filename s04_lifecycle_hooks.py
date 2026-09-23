"""s04: Lifecycle hooks —— 横切逻辑挂在循环周围，不写进循环里

课程：learn-claudecode s04
核心：日志 / 计时 / 审计这类"横切关注点"，注册成钩子挂在循环的关键事件点上，
      而不是塞进循环体。加功能 = 加一个钩子，循环主干不动。
"""
import os
import time
import subprocess
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ───────── 工具 ─────────
@tool
def get_current_time() -> str:
    """Get the current time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@tool
def run_bash(command: str) -> str:
    """执行一条 shell 命令并返回输出。仅用于查看系统/文件信息。"""
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        return f"exit={r.returncode}\n{r.stdout}{r.stderr}".strip()
    except subprocess.TimeoutExpired:
        return "命令执行超时"

# ───────── 单一数据源 ─────────
tools = [get_current_time, add, run_bash]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}

# ═══════════════ 钩子系统 ═══════════════
class Hooks:
    def __init__(self):
        self._handlers = {}                       # {事件名: [回调, ...]}

    def on(self, event):
        """装饰器：把一个函数注册到某个事件点"""
        def deco(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return deco

    def emit(self, event, **data):
        """触发事件：依次调用所有注册到该事件的钩子"""
        for fn in self._handlers.get(event, []):
            fn(**data)

hooks = Hooks()

# ───────── 注册钩子（横切逻辑全在这里，循环体不在其中）─────────
@hooks.on("loop_start")
def hook_log_start(query):
    print(f"    [hook:loop_start]   收到问题：{query}")

@hooks.on("before_model")
def hook_log_model(round_no, messages):
    print(f"    [hook:before_model] 第 {round_no} 轮，上下文 {len(messages)} 条")

@hooks.on("before_tool")
def hook_log_tool_start(name, args):
    print(f"    [hook:before_tool]  即将执行 {name}({args})")

@hooks.on("after_tool")
def hook_log_tool_end(name, duration):
    print(f"    [hook:after_tool]   {name} 完成，耗时 {duration:.2f}s")

@hooks.on("tool_denied")
def hook_log_denied(name):
    print(f"    [hook:tool_denied]  {name} 被权限门拒绝")

@hooks.on("loop_end")
def hook_summary(rounds, total_time):
    print(f"\n    [hook:loop_end] 共 {rounds} 轮，总耗时 {total_time:.2f}s")

# ───────── 权限门（s03 的成果）─────────
DANGEROUS = {"run_bash"}

def check_permission(tool_name: str, args: dict) -> bool:
    if tool_name not in DANGEROUS:
        return True
    if os.environ.get("AUTO_APPROVE") == "1":
        print(f"  [权限门] {tool_name}({args}) → 自动批准")
        return True
    try:
        ans = input(f"  [权限门] ⚠️  允许执行 {tool_name}({args}) 吗？[y/N] ")
    except EOFError:
        ans = ""
    return ans.strip().lower() in ("y", "yes")

# ═══════════════ Agent Loop（主干只剩「调模型 → 执行工具」）═══════════════
query = "帮我看看当前目录有哪些文件，再告诉我现在几点"
messages = [HumanMessage(content=query)]

t_start = time.time()
round_no = 0
hooks.emit("loop_start", query=query)

while True:
    round_no += 1

    hooks.emit("before_model", round_no=round_no, messages=messages)
    response = llm_with_tools.invoke(messages)
    messages.append(response)
    hooks.emit("after_model", round_no=round_no, response=response)

    if not response.tool_calls:
        print(f"[第 {round_no} 轮] 模型不再调工具 → 结束")
        print("最终答案:", response.content)
        break

    for tc in response.tool_calls:
        print(f"[第 {round_no} 轮] 要调工具: {tc['name']}({tc['args']})")

        if not check_permission(tc["name"], tc["args"]):
            hooks.emit("tool_denied", name=tc["name"])
            messages.append(ToolMessage(
                content="操作被拒绝：当前无权限执行该工具。",
                tool_call_id=tc["id"]))
            continue

        hooks.emit("before_tool", name=tc["name"], args=tc["args"])
        t0 = time.time()
        result = TOOLS[tc["name"]].invoke(tc["args"])          #  ← 主干只有这一行执行
        hooks.emit("after_tool", name=tc["name"], duration=time.time() - t0)

        messages.append(ToolMessage(
            content=str(result), tool_call_id=tc["id"]))

hooks.emit("loop_end", rounds=round_no, total_time=time.time() - t_start)
