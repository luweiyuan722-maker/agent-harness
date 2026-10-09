"""测试 s09 模块 5（提取）+ 模块 6（去重）"""
import sys
import shutil
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage

sys.path.insert(0, str(Path.cwd()))
import s09_memory_full as s   # noqa: E402

shutil.rmtree(".memory", ignore_errors=True)

print("=" * 55)
print("测试 1：extract_memories 提取记忆（调 LLM）")
print("=" * 55)
messages = [
    HumanMessage(content="帮我写个 C++ 函数"),
    AIMessage(content="好的，用什么缩进风格？"),
    HumanMessage(content="我写 C++ 习惯用 4 个空格缩进，不用 tab，以后都这样"),
    AIMessage(content="明白，就用 4 空格"),
]
found = s.extract_memories(messages)
print(f"  提取到 {len(found)} 条：")
for item in found:
    print(f"    - {item['name']} ({item['mem_type']}): {item['description']}")
    print(f"      body 含 Why: {'Why' in item['body']}, 含 How: {'How to apply' in item['body']}")
assert found, "❌ 没提取到记忆"
assert all("Why" in it["body"] and "How to apply" in it["body"] for it in found), "❌ 缺 Why/How"
print("  ✅ 提取成功，格式完整")

print("\n" + "=" * 55)
print("测试 2：写入提取到的记忆")
print("=" * 55)
for item in found:
    s.write_memory_file(item["name"], item["mem_type"], item["description"], item["body"])
print(f"  记忆库现在有 {len(s.list_memory_files())} 条")

print("\n" + "=" * 55)
print("测试 3：find_duplicate 识别重复（不同措辞）")
print("=" * 55)
dup = s.find_duplicate("C++ 缩进用四个空格", "用户 C++ 代码统一 4 空格缩进")
print(f"  查重结果: {dup!r}")
assert dup, "❌ 没识别出重复"
print("  ✅ 识别出重复")

print("\n" + "=" * 55)
print("测试 4：write_memory_file 去重（更新而非新建）")
print("=" * 55)
before = len(s.list_memory_files())
s.write_memory_file("C++ 缩进用四个空格", "user",
                    "用户 C++ 代码统一 4 空格缩进",
                    "用 4 空格。**Why:** 风格。**How to apply:** 写代码用 4 空格。")
after = len(s.list_memory_files())
print(f"  写入前后条数: {before} -> {after}")
assert after == before, "❌ 重复记忆被新建了"
print("  ✅ 去重生效（更新而非新建）")
