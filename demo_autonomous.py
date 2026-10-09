import importlib.util
import time
import shutil

spec = importlib.util.spec_from_file_location(
    "s17", "/Users/shikanoko/Desktop/agent_harness/s17_autonomous_agents.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shutil.rmtree(mod.TASKS_DIR, ignore_errors=True)
shutil.rmtree(mod.MAILBOX_DIR, ignore_errors=True)

# ── Lead 创建 3 个任务 ──
mod.create_task.invoke({"subject": "创建 database.py，含一个简单的 User 类"})
mod.create_task.invoke({"subject": "创建 api.py，含一个 hello 函数"})
mod.create_task.invoke({"subject": "创建 test.py，含一个测试函数"})
print("Lead 创建了 3 个任务到任务板")

# ── spawn alice 和 bob ──
mod.spawn_teammate_thread("alice", "backend developer",
                          "你是后端开发者，完成任务板的认领任务")
mod.spawn_teammate_thread("bob", "backend developer",
                          "你是后端开发者，完成任务板的认领任务")
print("启动了 alice 和 bob，等待自动认领干活...\n")

# ── 等队友自动认领 + 干活（40 秒）──
time.sleep(40)
print("=== 任务板（干活中）===")
print(mod.list_tasks.invoke({}))

# ── 手动 shutdown，让队友退出并发 summary ──
mod.BUS.send("lead", "alice", "请关机", "shutdown_request", {"request_id": "req_alice"})
mod.BUS.send("lead", "bob", "请关机", "shutdown_request", {"request_id": "req_bob"})
time.sleep(4)

print("\n=== 任务板最终状态 ===")
print(mod.list_tasks.invoke({}))
print("\n=== Lead 收件箱（队友 summary）===")
inbox = mod.BUS.read_inbox("lead")
result_msgs = [m for m in inbox if m["type"] == "result"]
for m in result_msgs:
    print(f"  from={m['from']}: {m['content'][:150]}")
