"""s06: Subagent —— 大任务拆小，子任务用干净的上下文

课程：learn-claudecode s06
核心：子 agent 是一个【独立的循环 + 独立的 messages】。它干完只把【结论】交回主线，
      中间几十轮的探索细节留在它自己那里，用完即弃 —— 主线上下文保持干净。
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

# ───────── 基础工具（主子 agent 都能用）─────────
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

BASE_TOOLS = [get_current_time, run_bash]
SUB_TOOLS = {t.name: t for t in BASE_TOOLS}          # 子 agent 的分派表（无 delegate → 防递归）

# ═══════════ 子 agent：一个独立的循环 ═══════════
llm_sub = llm.bind_tools(BASE_TOOLS)                 # ★ 子 agent 只能看到基础工具（无 delegate）

def run_subagent(task: str) -> str:
    """跑一个独立的子 agent 循环，返回它的最终结论（不返回中间过程）"""
    print(f"    ┌─ 子 agent 启动：{task[:44]}…")
    sub_messages = [HumanMessage(content=task)]      # ★ 独立的 messages，与主线无关
    step = 0
    while True:
        step += 1
        resp = llm_sub.invoke(sub_messages)
        sub_messages.append(resp)

        if not resp.tool_calls:
            print(f"    └─ 子 agent 完成：内部跑了 {step} 轮 / 攒了 {len(sub_messages)} 条消息，"
                  f"只把结论回传主线")
            return resp.content

        for tc in resp.tool_calls:
            print(f"       [子] {tc['name']}({str(tc['args'])[:48]})")
            result = SUB_TOOLS[tc["name"]].invoke(tc["args"])
            sub_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

# ═══════════ 把子 agent 包装成工具，给主线用 ═══════════
@tool
def delegate(task: str) -> str:
    """把一件【独立的、需要多步工具探索的】子任务交给子 agent 去做，返回它的结论。
    简单问题（如"现在几点"）自己直接回答，不要用这个工具。"""
    return run_subagent(task)

MAIN_TOOLS = BASE_TOOLS + [delegate]                 # ★ 只有主线能派活
llm_main = llm.bind_tools(MAIN_TOOLS)
TOOLS = {t.name: t for t in MAIN_TOOLS}

# ───────── 权限门 ─────────
DANGEROUS = {"run_bash"}

def check_permission(name, args):
    if name not in DANGEROUS:
        return True
    return os.environ.get("AUTO_APPROVE") == "1"

# ═══════════ 主线 Agent Loop ═══════════
query = ("帮我调查两件独立的事，分别处理完再汇总："
         "① 当前目录一共有多少个 .py 文件；"
         "② requirements.txt 里声明了哪些依赖。")
messages = [HumanMessage(content=query)]

round_no = 0
t0 = time.time()
while True:
    round_no += 1
    print(f"\n──── 主线第 {round_no} 轮 ────")

    response = llm_main.invoke([SystemMessage(content="你是主控 agent。遇到独立的复杂子任务，用 delegate 交给子 agent 处理。")] + messages)
    messages.append(response)

    if not response.tool_calls:
        print("\n══════ 主线的最终答案 ══════")
        print(response.content)
        break

    for tc in response.tool_calls:
        print(f"    → 主线调用 {tc['name']}({str(tc['args'])[:60]})")
        if not check_permission(tc["name"], tc["args"]):
            messages.append(ToolMessage(content="操作被拒绝。", tool_call_id=tc["id"]))
            continue
        result = TOOLS[tc["name"]].invoke(tc["args"])
        messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

print(f"\n总耗时 {time.time() - t0:.1f}s")
print(f"主线 messages 条数：{len(messages)}（如果没有子 agent，这里会多出很多条工具细节）")
