import importlib.util
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "s16", "/Users/shikanoko/Desktop/agent_harness/lessons/s16_team_protocols.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── 第 1 步：spawn alice 建文件（不带关机）──
mod.lead_loop("Spawn alice as a backend developer, ask her to create config.py.")
print("等 alice 干活（建 config.py → 发 summary → 进 idle）...")
time.sleep(20)
print(f"  config.py 存在: {Path('config.py').exists()}")

# ── 第 2 步：请求关机（手动走握手）──
req_id = mod.new_request_id()
mod.pending_requests[req_id] = mod.ProtocolState(
    req_id, "shutdown", "lead", "alice", "pending", "", time.time())
mod.BUS.send("lead", "alice", "请收尾并关机", "shutdown_request", {"request_id": req_id})
print(f"\n已发 shutdown_request（{req_id}），等 alice idle 收到并回复...")
time.sleep(5)

# ── 第 3 步：Lead 消费 + 匹配 ──
lead_inbox = mod.consume_lead_inbox()
print(f"\n=== 关机握手结果 ===")
print(f"  Lead 收件箱收到: {[m['type'] for m in lead_inbox]}")
print(f"  shutdown 状态: {mod.pending_requests[req_id].status}")
print(f"  config.py 完整: {Path('config.py').exists()}")
