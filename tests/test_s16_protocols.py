import importlib.util
import time
import shutil

spec = importlib.util.spec_from_file_location(
    "s16", "/Users/shikanoko/Desktop/agent_harness/lessons/s16_team_protocols.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shutil.rmtree(mod.MAILBOX_DIR, ignore_errors=True)

# ── 1. new_request_id 唯一 + 格式 ──
ids = {mod.new_request_id() for _ in range(100)}
assert len(ids) == 100, "100 次应全唯一"
rid = mod.new_request_id()
assert rid.startswith("req_") and len(rid) == len("req_") + 6
print("✅ new_request_id：req_ 前缀 + 100 次唯一")

# ── 2. match_response 正常关联 ──
rid = mod.new_request_id()
mod.pending_requests[rid] = mod.ProtocolState(
    rid, "shutdown", "lead", "alice", "pending", "", time.time())
mod.match_response("shutdown_response", rid, True)
assert mod.pending_requests[rid].status == "approved"
print("✅ 正常关联：shutdown pending → approved")

# ── 3. 类型校验（防串扰）──
rid2 = mod.new_request_id()
mod.pending_requests[rid2] = mod.ProtocolState(
    rid2, "shutdown", "lead", "alice", "pending", "", time.time())
mod.match_response("plan_approval_response", rid2, True)   # 类型不对
assert mod.pending_requests[rid2].status == "pending", "应保持不变"
print("✅ 类型校验：plan_approval_response 不能 approve shutdown 请求")

# ── 4. 重复处理（防重复）──
mod.match_response("shutdown_response", rid, False)   # 已 approved，再发 reject
assert mod.pending_requests[rid].status == "approved", "已处理不该再变"
print("✅ 重复处理：approved 后再次 match 状态不变")

# ── 5. 不存在 ID ──
mod.match_response("shutdown_response", "req_999999", True)   # 不抛异常
print("✅ 不存在 ID：静默忽略，不抛异常")

# ── 6. 完整关机握手（BUS 层，不依赖 API）──
rid3 = mod.new_request_id()
mod.pending_requests[rid3] = mod.ProtocolState(
    rid3, "shutdown", "lead", "alice", "pending", "", time.time())
mod.BUS.send("lead", "alice", "请关机", "shutdown_request", {"request_id": rid3})
inbox = mod.BUS.read_inbox("alice")
assert inbox[0]["type"] == "shutdown_request"
assert inbox[0]["metadata"]["request_id"] == rid3
# 队友回复
mod.BUS.send("alice", "lead", "已收尾", "shutdown_response",
             {"request_id": rid3, "approve": True})
# Lead 消费 + 匹配
lead_inbox = mod.consume_lead_inbox()
assert len(lead_inbox) == 1
assert mod.pending_requests[rid3].status == "approved"
print("✅ 完整握手：请求→回复→consume→match→approved")

print("\n════════ 全部通过 ════════")
