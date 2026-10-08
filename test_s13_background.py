import importlib.util
import time

spec = importlib.util.spec_from_file_location(
    "s13", "/Users/shikanoko/Desktop/agent_harness/s13_Background_Tasks.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── 1. should_run_background：显式优先 + 启发式兜底 ──
assert mod.should_run_background("bash", {"command": "ls", "run_in_background": True}) is True
assert mod.should_run_background("bash", {"command": "npm install"}) is True     # 启发式命中
assert mod.should_run_background("bash", {"command": "ls -la"}) is False         # 快命令
assert mod.should_run_background("create_task", {"subject": "x"}) is False       # 非 bash
print("✅ should_run_background：显式=True / npm install=True / ls=False / 非bash=False")

# ── 2. start_background_task 不阻塞 + collect 通知 + 防重复 ──
t0 = time.time()
bg_id = mod.start_background_task("run_bash", {"command": "sleep 1 && echo done"}, "tool_001")
elapsed = time.time() - t0
assert elapsed < 0.5, f"应该立即返回，实际等了 {elapsed:.2f}s"
print(f"✅ start_background_task 立即返回 bg_id={bg_id}（耗时 {elapsed:.3f}s，没等 1 秒）")

assert mod.collect_background_results() == []      # 立刻收集：还没完成
print("✅ 立刻 collect：还没完成，返回空 []")

time.sleep(1.5)                                    # 等后台跑完
notifs = mod.collect_background_results()
assert len(notifs) == 1, f"应 1 条通知，实际 {len(notifs)}"
assert "<task_notification>" in notifs[0]
assert "done" in notifs[0]
print("✅ 完成后 collect：1 条 task_notification，含后台输出 'done'")

assert mod.collect_background_results() == []      # 再收集：已 pop，防重复
print("✅ 再 collect：已 pop，返回空（防重复收集）")

print("\n════════ 全部通过 ════════")
