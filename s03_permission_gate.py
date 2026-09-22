"""s03: Permission gate —— 危险操作执行前的决策点

课程：learn-claudecode s03
核心：工具不是无条件执行的。执行之前，harness 要先判断"这个操作允许吗"。
"""
import os
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

# ───────── 单一数据源（s02 的成果）─────────
tools = [get_current_time, add, run_bash]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}

# ───────── 权限门 ─────────
# 危险工具清单：执行前必须过决策点
DANGEROUS = {"run_bash"}

def check_permission(tool_name: str, args: dict) -> bool:
    """决策点：返回 True = 允许执行，False = 拒绝。"""
    if tool_name not in DANGEROUS:
        return True                                     # 安全工具：直接放行
    if os.environ.get("AUTO_APPROVE") == "1":           # 演示/测试用
        print(f"  [权限门] 危险操作 {tool_name}({args}) → 自动批准（AUTO_APPROVE=1）")
        return True
    try:
        ans = input(f"  [权限门] ⚠️  模型请求危险操作 {tool_name}({args})，允许吗？[y/N] ")
    except EOFError:
        ans = ""
    return ans.strip().lower() in ("y", "yes")

# ───────── Agent Loop ─────────
query = "帮我看看当前目录有哪些文件，再告诉我现在几点"
messages = [HumanMessage(content=query)]

round_no = 0
while True:
    round_no += 1
    print(f"\n[第 {round_no} 轮] 调模型…")
    response = llm_with_tools.invoke(messages)
    messages.append(response)

    if not response.tool_calls:
        print(f"[第 {round_no} 轮] 模型不再调工具 → 结束")
        print("最终答案:", response.content)
        break

    for tc in response.tool_calls:
        print(f"[第 {round_no} 轮] 要调工具: {tc['name']}({tc['args']})")

        # ★ 权限门：执行之前先决策 ★
        if not check_permission(tc["name"], tc["args"]):
            print(f"[第 {round_no} 轮] ⛔ 已拒绝执行: {tc['name']}")
            messages.append(ToolMessage(
                content="操作被拒绝：当前无权限执行该工具。",
                tool_call_id=tc["id"]))
            continue                                  # 跳过执行，但循环继续

        result = TOOLS[tc["name"]].invoke(tc["args"])
        print(f"[第 {round_no} 轮] 工具返回: {result}")
        messages.append(ToolMessage(
            content=str(result), tool_call_id=tc["id"]))
