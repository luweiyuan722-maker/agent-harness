"""测试 s09 模块 1：存储层"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import s09_memory_full as s   # noqa: E402

# 清空记忆库
shutil.rmtree(".memory", ignore_errors=True)

print("=" * 50)
print("测试 1：写两条记忆（英文名 + 中文名）")
print("=" * 50)
p1 = s.write_memory_file("User Prefers Tabs", "user", "User prefers tabs", "正文：用 tab 不用空格")
p2 = s.write_memory_file("别 mock 数据库", "feedback", "别 mock 数据库", "正文：测试别 mock")
print("写入 1:", p1)
print("写入 2:", p2)

print("\n" + "=" * 50)
print("测试 2：list_memory_files（应排序：n 在 u 前）")
print("=" * 50)
for m in s.list_memory_files():
    print(m)

print("\n" + "=" * 50)
print("测试 3：索引 MEMORY.md 内容（应 2 行，不含自己）")
print("=" * 50)
print(s.INDEX_FILE.read_text(encoding="utf-8"))

print("=" * 50)
print("测试 4：_slugify 边界")
print("=" * 50)
for name in ["User Prefers Tabs", "C++ Coding Style", "  首尾有空格  ", "用户偏好", ""]:
    print(f"  {name!r:22} -> {s._slugify(name)!r}")

print("\n" + "=" * 50)
print("测试 5：同名覆盖（slug 相同应覆盖而非新建）")
print("=" * 50)
s.write_memory_file("User Prefers Tabs", "user", "更新后的描述", "新正文")
files = s.list_memory_files()
print(f"  文件数（应还是 2）: {len(files)}")
print(f"  tabs 那条的描述（应变成'更新后的描述'）: "
      f"{[m['description'] for m in files if 'tabs' in m['filename']]}")

print("\n" + "=" * 50)
print("测试 6：空库边界（目录不存在时 list 不崩）")
print("=" * 50)
shutil.rmtree(".memory", ignore_errors=True)
print(f"  空库 list_memory_files -> {s.list_memory_files()}")
