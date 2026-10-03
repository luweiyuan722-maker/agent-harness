"""测试 s09 模块 3：read_memory_body + side-query + 注入"""
import sys
import shutil
from pathlib import Path
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path.cwd()))
import s09_memory_full as s   # noqa: E402

shutil.rmtree(".memory", ignore_errors=True)

# 写 3 条不同领域的记忆
s.write_memory_file(
    "User Prefers Tabs", "user", "用户写代码习惯用 tab 缩进",
    "用户要求用 tab 缩进。**Why:** 和现有代码库一致。**How to apply:** 写文件时用 tab。")
s.write_memory_file(
    "别 mock 数据库", "feedback", "测试时别 mock 数据库",
    "测试时不要 mock 数据库，用真实连接。**Why:** mock 会掩盖真实问题。**How to apply:** 写测试时用真库。")
s.write_memory_file(
    "C++ 用 RAII", "reference", "C++ 内存管理用 RAII",
    "C++ 用 RAII 管理内存。**Why:** 防泄漏。**How to apply:** 用智能指针。")

print("=" * 55)
print("测试 1：read_memory_body（纯本地，不调 LLM）")
print("=" * 55)
body = s.read_memory_body("user-prefers-tabs.md")
print(body)
assert "tab" in body and "Why" in body, "❌ 正文没读出来"
print("  ✅ 正文读取正确")

print("\n" + "=" * 55)
print("测试 2：select_relevant_memories（调 LLM）")
print("=" * 55)
messages = [HumanMessage(content="帮我检查这段 C++ 代码有没有内存泄漏")]
picked = s.select_relevant_memories(messages)
print(f"  选中: {picked}")
# 期望：至少选中 C++ RAII（内存相关）；"tab 缩进"不该被选
assert picked, "❌ 一条都没选中"
print("  ✅ 筛选出相关记忆")

print("\n" + "=" * 55)
print("测试 3：build_memory_injection（拼装注入块）")
print("=" * 55)
injection = s.build_memory_injection(messages)
print(injection)
assert "<相关记忆>" in injection and "</相关记忆>" in injection, "❌ 缺标记"
print("  ✅ 注入块格式正确")

print("\n" + "=" * 55)
print("测试 4：空库边界（不该调 LLM，直接返回空）")
print("=" * 55)
shutil.rmtree(".memory", ignore_errors=True)
print(f"  select -> {s.select_relevant_memories(messages)}")
print(f"  injection -> {s.build_memory_injection(messages)!r}")
