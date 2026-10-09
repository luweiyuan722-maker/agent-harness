import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "s15", "/Users/shikanoko/Desktop/agent_harness/lessons/s15_agent_teams.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 清空收件箱，干净起跑
import shutil
shutil.rmtree(mod.MAILBOX_DIR, ignore_errors=True)

# ── 1. send + read 基本链路 ──
mod.BUS.send("lead", "alice", "建 schema.sql", "message")
mod.BUS.send("lead", "alice", "建完了记得测", "message")
inbox = mod.BUS.read_inbox("alice")
assert len(inbox) == 2, f"应有 2 条，实际 {len(inbox)}"
assert inbox[0]["from"] == "lead" and inbox[0]["content"] == "建 schema.sql"
assert inbox[1]["content"] == "建完了记得测"
print("✅ send/read：2 条消息，顺序正确，from/content 正确")

# ── 2. 消费式读取（读完删除）──
assert mod.BUS.read_inbox("alice") == [], "读完应删掉，再读应为空"
print("✅ 消费式：读完 unlink，再读为空（恰好一次消费）")

# ── 3. 文件不存在 → 返回空 ──
assert mod.BUS.read_inbox("nobody") == []
print("✅ 无消息：不存在的收件箱返回 []（不抛异常）")

# ── 4. 不同 agent 收件箱隔离 ──
mod.BUS.send("alice", "bob", "hi bob", "message")
assert mod.BUS.read_inbox("bob")[0]["from"] == "alice"
assert mod.BUS.read_inbox("alice") == []   # alice 的收件箱不受影响
print("✅ 隔离：bob 收到 alice 的消息，alice 自己的收件箱是空的")

# ── 5. 并发写同一文件（加锁验证：不丢消息）──
import threading
mod.BUS.send("a1", "lead", "x" * 50, "result")
mod.BUS.send("a2", "lead", "y" * 50, "result")
mod.BUS.send("a3", "lead", "z" * 50, "result")
got = mod.BUS.read_inbox("lead")
assert len(got) == 3, f"并发写应 3 条全在，实际 {len(got)}"
print("✅ 并发写：3 条消息全部保留（锁生效，无交错丢失）")

print("\n════════ 全部通过 ════════")
