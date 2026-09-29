"""s05: Todo manager —— 让 agent 显式管理自己的计划

课程：learn-claudecode s05
核心：计划是循环【外】的独立状态。用 todo_write 工具写入，每轮再注入给模型。
      好处：计划【可见】（实时看进度）、【可纠正】（跑偏了能改）。
"""
import os
import time
import subprocess
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ═══════════ 计划状态：存在循环【外】的独立变量 ═══════════
todos: list[str] = []

def render_todos() -> str:# 渲染当前计划
    if not todos:
        return "（暂无计划）"
    return "\n".join(f"  {i+1}. {t}" for i, t in enumerate(todos))

# ───────── 工具 ─────────
@tool
def get_current_time() -> str:
    """Get the current time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool
def run_bash(command: str) -> str:
    """执行一条 shell 命令并返回输出。仅用于查看系统/文件信息。"""
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        return f"exit={r.returncode}\n{r.stdout}{r.stderr}".strip()
    except subprocess.TimeoutExpired:
        return "命令执行超时"

@tool
def todo_write(items: list[str]) -> str:
    """创建或更新任务计划。传入【完整】的新计划列表（字符串数组），整体替换旧计划。
    每项格式："[ ] 待做" 或 "[x] 已完成"。例如 ["[ ] 查看目录", "[x] 统计文件数"]。"""
    global todos# 全局变量，需要声明为 global 才能修改
    todos = list(items)# 更新计划
    return "计划已更新：\n" + render_todos()

# ───────── 单一数据源（s02 的成果）─────────
tools = [get_current_time, run_bash, todo_write]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}

# ───────── 钩子（s04 的成果）─────────
class Hooks:
    def __init__(self):
        self._handlers = {}
    def on(self, event):
        def deco(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return deco
    def emit(self, event, **data):
        for fn in self._handlers.get(event, []):
            fn(**data)

hooks = Hooks()

@hooks.on("before_model")
def show_plan(round_no):
    print(f"\n──── 第 {round_no} 轮 · 当前计划 ────")
    print(render_todos())

@hooks.on("after_tool")
def show_tool_done(name, duration):
    print(f"      ✓ {name} ({duration:.2f}s)")

# ───────── 权限门（s03 的成果）─────────
DANGEROUS = {"run_bash"}

def check_permission(name, args):
    if name not in DANGEROUS:
        return True
    if os.environ.get("AUTO_APPROVE") == "1":
        return True
    try:
        ans = input(f"  [权限门] 允许执行 {name}({args}) 吗？[y/N] ")
    except EOFError:
        ans = ""
    return ans.strip().lower() in ("y", "yes")

# ═══════════ Agent Loop ═══════════
query = ("帮我完成三件事：① 看看当前目录有哪些文件；"
         "② 数一数其中 .py 文件有多少个；③ 告诉我现在的时间。")
messages = [HumanMessage(content=query)]

SYSTEM_TMPL = """你是任务执行助手。

【当前计划】
{todos}

【工作规则】
1. 接到复杂任务，先用 todo_write 制定计划（每项写成 "[ ] 描述"）
2. 每完成一步，立刻调 todo_write 更新（把对应项改成 "[x] 描述"）
3. 简单问题（如"现在几点"）不必制定计划，直接回答
"""

round_no = 0
t0 = time.time()
while True:
    round_no += 1
    hooks.emit("before_model", round_no=round_no)

    # ★ 每轮把【最新计划】注入上下文 —— 计划不在 messages 里，是独立状态
    convo = [SystemMessage(content=SYSTEM_TMPL.format(todos=render_todos()))] + messages
    response = llm_with_tools.invoke(convo)
    messages.append(response)

    if not response.tool_calls:
        print("\n══════ 最终答案 ══════")
        print(response.content)
        break

    for tc in response.tool_calls:
        print(f"    → 调用 {tc['name']}({str(tc['args'])[:90]})")
        if not check_permission(tc["name"], tc["args"]):
            messages.append(ToolMessage(content="操作被拒绝。", tool_call_id=tc["id"]))
            continue
        t = time.time()
        result = TOOLS[tc["name"]].invoke(tc["args"])
        hooks.emit("after_tool", name=tc["name"], duration=time.time() - t)
        messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

print(f"\n总耗时 {time.time() - t0:.1f}s")
