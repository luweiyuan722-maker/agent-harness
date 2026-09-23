"""演示：技能调用在哪些地方留下痕迹（对应 s07）"""
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage

load_dotenv()
llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ── 复用 s07 的技能加载 ──
def load_skills():
    skills = {}
    for d in sorted(Path("skills").iterdir()):
        f = d / "SKILL.md"
        if not (d.is_dir() and f.exists()):
            continue
        content = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
        name = re.search(r"name:\s*(.+)", m.group(1))
        desc = re.search(r"description:\s*(.+)", m.group(1))
        skills[name.group(1).strip()] = {"description": desc.group(1).strip(), "body": m.group(2).strip()}
    return skills

SKILLS = load_skills()
catalog = "\n".join(f"- {n}: {v['description']}" for n, v in SKILLS.items())

@tool
def load_skill(name: str) -> str:
    """加载某个技能的完整内容。"""
    return SKILLS[name]["body"] if name in SKILLS else f"无此技能：{name}"

TOOLS = {"load_skill": load_skill}
llm_with_tools = llm.bind_tools([load_skill])

print("【痕迹 1】system prompt 里的技能目录（启动时就拼好的）")
print("-" * 60)
print(catalog)
print("-" * 60)

# ── 跑一轮：模型决定加载技能 ──
sys_msg = SystemMessage(content=f"可用技能（需要时用 load_skill 加载）：\n{catalog}")
messages = [sys_msg, HumanMessage(content="帮我审查这段 C++ 代码：void f(){int* p=new int(5); if(*p>3) return; delete p;}")]

resp = llm_with_tools.invoke(messages)
messages.append(resp)

print("\n【痕迹 2】模型这次的 tool_calls（它决定加载哪个技能）")
print("   ", resp.tool_calls)

tc = resp.tool_calls[0]
messages.append(ToolMessage(content=TOOLS[tc["name"]].invoke(tc["args"]), tool_call_id=tc["id"]))

print("\n【痕迹 3】加载后，messages 里的内容一览：")
print("-" * 60)
for i, m in enumerate(messages):
    preview = str(m.content).replace("\n", " ")[:55]
    print(f"  [{i}] {type(m).__name__:14} | {len(str(m.content)):>4} 字符 | {preview}…")
print("-" * 60)

print("\n【痕迹 4】技能正文现在躺在 messages[2] 里（前 180 字）：")
print("-" * 60)
print(messages[2].content[:180], "…")
print("-" * 60)
