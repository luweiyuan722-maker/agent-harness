import importlib.util
import shutil
import threading
import json

spec = importlib.util.spec_from_file_location(
    "s17", "/Users/shikanoko/Desktop/agent_harness/s17_autonomous_agents.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shutil.rmtree(mod.TASKS_DIR, ignore_errors=True)
shutil.rmtree(mod.MAILBOX_DIR, ignore_errors=True)

# ── 1. 创建任务 + scan 三条件 ──
mod.create_task.invoke({"subject": "建数据库"})
mod.create_task.invoke({"subject": "写 API"})
mod.create_task.invoke({"subject": "写测试"})
tasks = list(mod.TASKS_DIR.glob("task_*.json"))
assert len(tasks) == 3, f"应有 3 个任务，实际 {len(tasks)}"

unclaimed = mod.scan_unclaimed_tasks()
assert len(unclaimed) == 3, f"3 个任务都 pending 无 owner，应全可认领，实际 {len(unclaimed)}"
print("✅ scan：3 个 pending 无 owner 任务全部可认领")

# ── 2. claim_task 正常认领 ──
first_id = unclaimed[0]["id"]
r = mod.claim_task(first_id, "alice")
assert "Claimed" in r, f"应认领成功，实际 {r}"
unclaimed2 = mod.scan_unclaimed_tasks()
assert len(unclaimed2) == 2, f"认领 1 个后应剩 2 个可认领，实际 {len(unclaimed2)}"
print("✅ claim：认领后任务从可认领列表消失（status 变 in_progress）")

# ── 3. owner/status 检查：已认领的不能再认领 ──
r2 = mod.claim_task(first_id, "bob")
assert "Claimed" not in r2, f"应拒绝（已被 alice 认领），实际 {r2}"
print(f"✅ 已认领拒绝：bob 认领被拒（{r2}）")

# ── 4. 并发认领：两个线程同时抢同一任务，只有一个成功 ──
target_id = unclaimed2[0]["id"]
results = []

def worker(name):
    results.append(mod.claim_task(target_id, name))

t1 = threading.Thread(target=worker, args=("alice",))
t2 = threading.Thread(target=worker, args=("bob",))
t1.start(); t2.start(); t1.join(); t2.join()

claimed = [r for r in results if "Claimed" in r]
rejected = [r for r in results if "Claimed" not in r]
assert len(claimed) == 1, f"并发抢应只有 1 个成功，实际 {len(claimed)}"
assert len(rejected) == 1, f"并发抢应只有 1 个被拒，实际 {len(rejected)}"
print(f"✅ 并发抢：{len(claimed)} 成功 / {len(rejected)} 被拒（锁 + owner 检查生效）")

# ── 5. 依赖：blockedBy 未完成不能认领 ──
# 用固定 id 的干净任务，避免被前面步骤污染
dep_task = mod.Task(id="task_dep01", subject="依赖任务", status="pending")
mod.save_task(dep_task)
blocked_task = mod.Task(id="task_block01", subject="被依赖任务", blockedBy=["task_dep01"])
mod.save_task(blocked_task)

# 依赖任务还是 pending（未完成）→ 被依赖任务不能认领
assert "Blocked" in mod.claim_task("task_block01", "carol"), "依赖未完成应拒绝"
# 依赖任务完成 → 现在能认领
dep_task.status = "completed"
mod.save_task(dep_task)
assert "Claimed" in mod.claim_task("task_block01", "carol"), "依赖完成后应能认领"
print("✅ 依赖检查：blockedBy 未完成拒绝认领，完成后可认领")

print("\n════════ 全部通过 ════════")
