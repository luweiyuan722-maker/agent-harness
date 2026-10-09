import importlib.util
import shutil

spec = importlib.util.spec_from_file_location(
    "s18", "/Users/shikanoko/Desktop/agent_harness/lessons/s18_worktree_isolation.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shutil.rmtree(mod.TASKS_DIR, ignore_errors=True)
shutil.rmtree(mod.WORKTREES_DIR, ignore_errors=True)

# ── 1. validate_worktree_name（防路径穿越）──
assert mod.validate_worktree_name("auth-refactor") == "", "合法名应通过"
assert mod.validate_worktree_name("../evil") != "", ".. 应拒绝"
assert mod.validate_worktree_name("a/b") != "", "斜杠应拒绝"
assert mod.validate_worktree_name("a b") != "", "空格应拒绝"
assert mod.validate_worktree_name("") != "", "空名应拒绝"
print("✅ validate：合法通过 / 路径穿越、斜杠、空格、空名全拒绝")

# ── 2. create_worktree（真实 git worktree add）──
r = mod.create_worktree("test-wt")
assert "已创建" in r, f"应创建成功，实际 {r}"
assert (mod.WORKTREES_DIR / "test-wt").exists()
print("✅ create_worktree：独立目录 + 分支创建成功")

# ── 3. bind_task_to_worktree ──
mod.create_task.invoke({"subject": "测试任务"})
tid = mod.scan_unclaimed_tasks()[0]["id"]
r = mod.bind_task_to_worktree(tid, "test-wt")
assert "已绑定" in r
t = mod.load_task(tid)
assert t.worktree == "test-wt" and t.status == "pending", "绑定后应仍 pending"
print("✅ bind：任务绑定 worktree，状态仍 pending（不影响认领）")

# ── 4. remove_worktree（有改动时拒绝）──
# 在 worktree 里写一个文件（制造未提交改动）
wt_file = mod.WORKTREES_DIR / "test-wt" / "changed.txt"
wt_file.write_text("改动了", encoding="utf-8")
r = mod.remove_worktree("test-wt", discard_changes=False)
assert "失败" in r or "error" in r.lower(), f"有改动应拒绝，实际 {r}"
print(f"✅ remove 拒绝：有改动时不删（{r[:50]}）")

# 强制删除
r = mod.remove_worktree("test-wt", discard_changes=True)
assert "已删除" in r
assert not (mod.WORKTREES_DIR / "test-wt").exists()
print("✅ remove 强制：discard_changes=True 删除成功")

# ── 5. 事件日志 ──
events = mod.WORKTREES_DIR / "events.jsonl"
assert events.exists()
lines = events.read_text().splitlines()
types = [__import__("json").loads(l)["type"] for l in lines]
assert "create" in types and "remove" in types
print(f"✅ 事件日志：记录了 {types}（审计）")

print("\n════════ 全部通过 ════════")
