import importlib.util
from datetime import datetime

spec = importlib.util.spec_from_file_location(
    "s14", "/Users/shikanoko/Desktop/agent_harness/s14_cron_scheduler.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ── 1. 基础匹配 ──
assert mod.cron_matches("* * * * *", datetime(2026, 10, 5, 12, 30)) is True
assert mod.cron_matches("0 9 * * *", datetime(2026, 10, 5, 9, 0)) is True
assert mod.cron_matches("0 9 * * *", datetime(2026, 10, 5, 10, 0)) is False
print("✅ 基础：* =任意 / 0 9 =9点 / 10点不匹配 9点")

# ── 2. */N 步长 ──
assert mod.cron_matches("*/5 * * * *", datetime(2026, 10, 5, 12, 5)) is True
assert mod.cron_matches("*/5 * * * *", datetime(2026, 10, 5, 12, 7)) is False
print("✅ */N：每5分钟，第5分钟命中/第7分钟不命中")

# ── 3. N-M 范围 ──
assert mod.cron_matches("0 9-17 * * *", datetime(2026, 10, 5, 14, 0)) is True
assert mod.cron_matches("0 9-17 * * *", datetime(2026, 10, 5, 18, 0)) is False
print("✅ N-M：9-17点，14点命中/18点不命中")

# ── 4. 星期编号转换（Python 周一=0 → cron 周日=0）──
# 2026-10-05 是周一 → cron dow=1
assert mod.cron_matches("0 9 * * 1", datetime(2026, 10, 5, 9, 0)) is True
assert mod.cron_matches("0 9 * * 1-5", datetime(2026, 10, 5, 9, 0)) is True
# 2026-10-11 是周日 → cron dow=0，不在 1-5
assert mod.cron_matches("0 9 * * 1-5", datetime(2026, 10, 11, 9, 0)) is False
print("✅ 星期：周一对 dow=1 / 工作日 1-5 命中 / 周日不命中")

# ── 5. DOM/DOW OR 语义（最易错）──
# "0 9 1 * 1" = 每月1号【或】周一 的9点（OR，不是 AND）
assert mod.cron_matches("0 9 1 * 1", datetime(2026, 10, 1, 9, 0)) is True   # 1号（周四，非周一）
assert mod.cron_matches("0 9 1 * 1", datetime(2026, 10, 5, 9, 0)) is True   # 周一（5号，非1号）
assert mod.cron_matches("0 9 1 * 1", datetime(2026, 10, 6, 9, 0)) is False  # 周二且非1号
print("✅ DOM/DOW OR：1号(周四)命中 / 周一(5号)命中 / 周二且非1号不命中")

# ── 6. 非法表达式 ──
assert mod.cron_matches("0 9 *", datetime(2026, 10, 5, 9, 0)) is False     # 4段
assert mod.cron_matches("", datetime(2026, 10, 5, 9, 0)) is False          # 空
print("✅ 非法：4段=False / 空=False（不抛异常）")

# ── 7. validate_cron ──
assert mod.validate_cron("0 9 * * *") == ""
assert mod.validate_cron("60 * * * *") != ""    # 分钟 60 超范围
assert mod.validate_cron("0 25 * * *") != ""    # 小时 25 超范围
assert mod.validate_cron("0 9 *") != ""         # 4 段
assert mod.validate_cron("abc * * * *") != ""   # 非数字
print("✅ validate：合法=空 / 超范围/段数错/非数字=报错")

# ── 8. schedule + list + cancel ──
r = mod.schedule_job("*/5 * * * *", "run date", durable=False)
assert "已注册" in r
job_id = list(mod.scheduled_jobs.keys())[0]
assert mod.scheduled_jobs[job_id].cron == "*/5 * * * *"
assert "已取消" in mod.cancel_job(job_id)
assert job_id not in mod.scheduled_jobs
print("✅ schedule/cancel：注册→字典有→取消→字典无")

print("\n════════ 全部通过 ════════")
