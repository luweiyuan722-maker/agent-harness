"""测试 s09 模块 4：降级机制"""
import sys
import shutil
from pathlib import Path
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path.cwd()))
import s09_memory_full as s   # noqa: E402

shutil.rmtree(".memory", ignore_errors=True)
s.write_memory_file("User Prefers Tabs", "user", "用户写代码习惯用 tab 缩进", "用户要求用 tab 缩进。")
s.write_memory_file("别 mock 数据库", "feedback", "测试时别 mock 数据库", "测试时不要 mock 数据库。")
s.write_memory_file("C++ 用 RAII", "reference", "C++ 内存管理用 RAII", "C++ 用 RAII 管理内存。")

messages = [HumanMessage(content="帮我检查这段 C++ 代码有没有内存泄漏")]

print("=" * 55)
print("测试 1：_keyword_fallback 单元测试（纯本地）")
print("=" * 55)
files = s.list_memory_files()
result = s._keyword_fallback(messages, files, 5)
print(f"  降级选中: {result}")
assert any("raii" in fn for fn in result), "❌ 没选中 RAII"
print("  ✅ 关键词匹配正确")

print("\n" + "=" * 55)
print("测试 2：模拟 side-query 失败 → 自动降级")
print("=" * 55)
class BoomLLM:
    def invoke(self, *args, **kwargs):
        raise RuntimeError("模拟 API 挂了")

original = s.llm
s.llm = BoomLLM()
try:
    picked = s.select_relevant_memories(messages)
finally:
    s.llm = original   # 恢复
print(f"  选中: {picked}")
assert picked, "❌ 降级后没选出记忆"
print("  ✅ 降级兜底生效")

print("\n" + "=" * 55)
print("测试 3：恢复正常后 side-query 还能工作")
print("=" * 55)
picked = s.select_relevant_memories(messages)
print(f"  选中: {picked}")
assert picked, "❌ 正常调用失败"
print("  ✅ 恢复正常")
