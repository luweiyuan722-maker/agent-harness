"""s07: On-demand skill loader —— 专业知识按需加载

课程：learn-claudecode s07
核心：技能正文【不】塞进 system prompt，只放【技能目录】（名字 + 一句话描述）。
      模型判断这次需要哪个 → 调 load_skill 加载正文 → 才进上下文。
      这就是"渐进式披露"（progressive disclosure）。
"""
import os
import re
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ═══════════ 扫描 skills/ 目录，只取【目录信息】 ═══════════
def load_skills() -> dict:
    """读 skills/<name>/SKILL.md 的 frontmatter，正文先存着但【不】注入 prompt"""
    skills = {}
    for d in sorted(Path("skills").iterdir()):
        f = d / "SKILL.md"
        if not (d.is_dir() and f.exists()):
            continue
        content = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)   # 切出 frontmatter 和正文
        if not m:
            continue
        front, body = m.group(1), m.group(2).strip()
        name = re.search(r"name:\s*(.+)", front)
        desc = re.search(r"description:\s*(.+)", front)
        skills[name.group(1).strip()] = {
            "description": desc.group(1).strip(),
            "body": body,
        }
    return skills

SKILLS = load_skills()

def skill_catalog() -> str:
    """只有这一小段目录会进 system prompt"""
    return "\n".join(f"- {n}: {v['description']}" for n, v in SKILLS.items())

_catalog = skill_catalog()
_body_total = sum(len(v["body"]) for v in SKILLS.values())
print("═══ 启动：技能库已加载 ═══")
print(_catalog)
print(f"→ 技能正文共 {_body_total} 字符，但进 prompt 的目录只有 {len(_catalog)} 字符 "
      f"（省了 {100 - len(_catalog) * 100 // _body_total}%）")
print()

# ───────── 工具 ─────────
@tool
def run_bash(command: str) -> str:
    """执行一条 shell 命令并返回输出。仅用于查看系统/文件信息。"""
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        return f"exit={r.returncode}\n{r.stdout}{r.stderr}".strip()
    except subprocess.TimeoutExpired:
        return "命令执行超时"

@tool
def load_skill(name: str) -> str:
    """加载某个技能的【完整内容】。当任务需要某项专业技能时（如 C++ 审查、SQL 优化）调用。
    name 必须是技能目录里列出的名字。"""
    if name not in SKILLS:
        return f"没有名为「{name}」的技能。可用技能：{', '.join(SKILLS)}"
    body = SKILLS[name]["body"]
    print(f"      ⤵ [load_skill] 注入技能「{name}」正文（{len(body)} 字符）")
    return body

tools = [run_bash, load_skill]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}

# ═══════════ Agent Loop ═══════════
SYSTEM_TMPL = """你是任务执行助手。

【可用技能】（需要时用 load_skill 加载完整内容）
{catalog}

【规则】
1. 如果任务匹配某个技能，必须先 load_skill 加载它，再严格按它的清单干活
2. 不匹配任何技能的任务，直接自己完成
"""

query = ("帮我审查这段 C++ 代码，找出所有问题：\n\n"
         "void process(int n) {\n"
         "    int* p = new int(n);\n"
         "    if (*p > 3) return;\n"
         "    delete p;\n"
         "}")

messages = [HumanMessage(content=query)]
round_no = 0
t0 = time.time()
while True:
    round_no += 1
    print(f"──── 第 {round_no} 轮 ────")

    convo = [SystemMessage(content=SYSTEM_TMPL.format(catalog=_catalog))] + messages
    response = llm_with_tools.invoke(convo)
    messages.append(response)

    if not response.tool_calls:
        print("\n══════ 最终答案 ══════")
        print(response.content)
        break

    for tc in response.tool_calls:
        print(f"    → 调用 {tc['name']}({str(tc['args'])[:70]})")
        result = TOOLS[tc["name"]].invoke(tc["args"])
        messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

print(f"\n总耗时 {time.time() - t0:.1f}s")
