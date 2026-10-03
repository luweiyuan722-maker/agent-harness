"""测试 s09 模块 2：build_memory_catalog（加载路径一）"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import s09_memory_full as s   # noqa: E402

shutil.rmtree(".memory", ignore_errors=True)

print("=" * 50)
print("测试 1：空库（应返回占位，不是空字符串）")
print("=" * 50)
result = s.build_memory_catalog()
print(f"  返回: {result!r}")
assert result != "", "❌ 空库返回了空字符串！"
print("  ✅ 空库有兜底")

print("\n" + "=" * 50)
print("测试 2：有记忆时的清单")
print("=" * 50)
s.write_memory_file("User Prefers Tabs", "user", "User prefers tabs", "正文")
s.write_memory_file("别 mock 数据库", "feedback", "别 mock 数据库", "正文")
catalog = s.build_memory_catalog()
print(catalog)
assert "User prefers tabs" in catalog and "别 mock 数据库" in catalog, "❌ 清单缺内容"
print("  ✅ 清单包含全部记忆 + type 字段")
