import importlib.util
import time

spec = importlib.util.spec_from_file_location(
    "s15", "/Users/shikanoko/Desktop/agent_harness/s15_agent_teams.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 派活（Lead 会 break，但 alice 线程还在后台跑）
mod.lead_loop("Spawn alice as a backend developer to create schema.sql "
              "with a users table.")
print("Lead 已派活，主线程等 20 秒让 alice 干完...")
time.sleep(20)                      # 关键：等 alice 干完，而不是立即退出

inbox = mod.BUS.read_inbox("lead")
print(f"\n收件箱收到 {len(inbox)} 条消息：")
for m in inbox:
    print(f"  from={m['from']}  type={m['type']}")
    print(f"  content={m['content'][:150]}")
