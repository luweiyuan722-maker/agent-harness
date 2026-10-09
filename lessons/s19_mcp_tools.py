"""s19: MCP Tool Bridge —— 外接工具，标准协议

课程：learn-claudecode s19
主旨：「发现、组装、调用，Agent 不需要知道工具是谁写的。」

真实企业化版（非 mock）：用 mcp 库连现成的官方 MCP server（stdio 子进程 + JSON-RPC）。

核心：
  RealMCPClient（真实连接 + tools/list 发现 + tools/call 调用）
  normalize_mcp_name（名称规范化，防冲突/注入）
  connect_mcp（连接外部 server，发现工具）
  mcp_call（统一调用入口，转发到真实 server）
"""
import json
import os
import re
import time
import asyncio
import threading
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()


# ═══════════ 内置工具（belt）═══════════
@tool
def run_bash(command: str) -> str:
    """执行 shell 命令。"""
    import subprocess
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


# ═══════════ 模块 1：RealMCPClient（真实连接）═══════════
class RealMCPClient:
    """真实 MCP 客户端：stdio 子进程 + JSON-RPC。

    mcp 库是 async 的，这里用「后台线程跑 event loop」封装成同步接口，
    这样 LangChain 的同步 invoke 能直接调用。
    """

    def __init__(self, name: str, command: str, args: list[str]):
        self.name = name
        self.tools: list = []            # 发现的工具列表
        self._session = None
        # 后台线程 + 独立 event loop
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        # 启动 _run 协程（async with 保持上下文，永不返回）
        asyncio.run_coroutine_threadsafe(self._run(command, args), self._loop)
        # 阻塞等连接完成（tools 被发现）
        while not self.tools:
            time.sleep(0.05)

    async def _run(self, command: str, args: list[str]):
        params = StdioServerParameters(command=command, args=args)
        # 用 async with 保持上下文（手动 __aenter__ 会让 anyio task-group 提前销毁）
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self.tools = result.tools
                self._session = session
                # park：永不返回，让 async with 的上下文在 call_tool 期间保持存活
                await asyncio.Event().wait()

    def call_tool(self, tool_name: str, args: dict) -> str:
        """同步调用工具：把 async 调用丢到后台 loop，阻塞等结果。"""
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, args), self._loop)
        result = fut.result(timeout=30)
        parts = [getattr(c, "text", "") for c in result.content]
        return "\n".join(p for p in parts if p)


# ═══════════ 模块 2：normalize_mcp_name ═══════════
_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    """非 [a-zA-Z0-9_-] 字符替换为 _，防命名冲突/注入。"""
    return _DISALLOWED.sub("_", name)


# ═══════════ 模块 3：connect_mcp + 工具描述 ═══════════
# 现成的 MCP server 目录（企业化：连真实的官方 server）
KNOWN_SERVERS = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem",
                 str(WORKDIR / ".mcptest")],
    },
}

mcp_clients: dict = {}


@tool
def connect_mcp(name: str) -> str:
    """连接一个 MCP server，发现它的工具。name 从 KNOWN_SERVERS 里选。"""
    if name in mcp_clients:
        return f"MCP server '{name}' 已连接"
    spec = KNOWN_SERVERS.get(name)
    if not spec:
        return f"未知 server '{name}'。可用：{', '.join(KNOWN_SERVERS)}"
    try:
        client = RealMCPClient(name, spec["command"], spec["args"])
    except Exception as e:
        return f"连接失败：{e}"
    mcp_clients[name] = client
    names = ", ".join(t.name for t in client.tools)
    return f"已连接 '{name}'，发现 {len(client.tools)} 个工具：{names}"


def build_mcp_tool_descriptions() -> str:
    """把所有已连接 MCP server 的工具描述拼成一段文本，注入 system prompt。"""
    if not mcp_clients:
        return "（未连接任何 MCP server）"
    lines = []
    for server_name, client in mcp_clients.items():
        for t in client.tools:
            safe = normalize_mcp_name(t.name)
            desc = (t.description or "")[:80]
            schema = t.input_schema
            if hasattr(schema, "model_dump"):       # Pydantic 模型 → dict
                schema = schema.model_dump()
            props = schema.get("properties", {})
            schema = ", ".join(props.keys()) if props else "无参数"
            lines.append(f"mcp__{normalize_mcp_name(server_name)}__{safe}: {desc}\n"
                         f"    参数: {schema}")
    return "\n".join(lines)


# ═══════════ 统一调用入口 ═══════════
@tool
def mcp_call(server: str, tool_name: str, arguments: str) -> str:
    """调用 MCP 工具。arguments 是 JSON 字符串（如 '{"path": "/tmp"}'）。"""
    client = mcp_clients.get(server)
    if not client:
        return f"未连接 server '{server}'，先调 connect_mcp"
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as e:
        return f"arguments 不是合法 JSON：{e}"
    return client.call_tool(tool_name, args)


BUILTIN_TOOLS = [run_bash, connect_mcp, mcp_call]
BUILTIN_MAP = {t.name: t for t in BUILTIN_TOOLS}


# ═══════════ Lead 循环（无缓存）═══════════
def lead_loop(query: str):
    messages = [HumanMessage(content=query)]
    llm_with_tools = llm.bind_tools(BUILTIN_TOOLS)
    while True:
        # 每次重新生成 system（无缓存：connect_mcp 后工具描述变了）
        mcp_desc = build_mcp_tool_descriptions()
        system = ("你是 Agent。内置工具：run_bash。可通过 connect_mcp 连接外部 MCP server，"
                  "用 mcp_call 调用其工具。\n\n当前可用的 MCP 工具：\n" + mcp_desc)
        response = llm_with_tools.invoke([SystemMessage(content=system)] + messages)
        messages.append(response)
        if not response.tool_calls:
            print(f"\n      💬 {response.content}")
            break
        for tc in response.tool_calls:
            result = BUILTIN_MAP[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


if __name__ == "__main__":
    lead_loop("Connect to the filesystem MCP server, then use it to list the "
              "current directory and create a file called hello_mcp.txt.")
