import importlib.util
import json

spec = importlib.util.spec_from_file_location(
    "s19", "/Users/shikanoko/Desktop/agent_harness/lessons/s19_mcp_tools.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── 1. normalize_mcp_name ──
assert mod.normalize_mcp_name("docs-server") == "docs-server"
assert mod.normalize_mcp_name("docs server") == "docs_server"   # 空格 → _
assert mod.normalize_mcp_name("a/b") == "a_b"                   # 斜杠 → _
assert mod.normalize_mcp_name("JIRA!") == "JIRA_"               # ! → _
print("✅ normalize：空格/斜杠/特殊字符全部 → _")

# ── 2. connect_mcp（真实连接官方 filesystem server）──
r = mod.connect_mcp.invoke({"name": "filesystem"})
assert "已连接" in r and "14" in r, f"应连接成功发现 14 工具，实际 {r[:80]}"
print(f"✅ connect_mcp：真实连接成功，发现 14 个工具")

# ── 3. mcp_call（真实调用 write_file）──
r = mod.mcp_call.invoke({
    "server": "filesystem",
    "tool_name": "write_file",
    "arguments": json.dumps({"path": "/Users/shikanoko/Desktop/agent_harness/.mcptest/test.txt",
                             "content": "via MCP"})})
print(f"✅ mcp_call：真实调用 write_file 成功")

# ── 4. mcp_call 读回验证 ──
r = mod.mcp_call.invoke({
    "server": "filesystem",
    "tool_name": "read_file",
    "arguments": json.dumps({"path": "/Users/shikanoko/Desktop/agent_harness/.mcptest/test.txt"})})
assert "via MCP" in r, f"读回应是 via MCP，实际 {r}"
print("✅ mcp_call：读回内容验证一致（via MCP）")

# ── 5. mcp_call 非法 JSON ──
r = mod.mcp_call.invoke({"server": "filesystem", "tool_name": "x", "arguments": "not json"})
assert "JSON" in r
print("✅ mcp_call：非法 JSON 参数报错")

print("\n════════ 全部通过 ════════")
