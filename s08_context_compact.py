"""s08: Context Compact —— 上下文压缩（四层管线 + 应急）

课程：learn-claudecode s08
主旨：「上下文总会满，要有办法腾地方」—— 四层压缩，便宜的先跑，贵的后跑。

  L3 tool_result_budget — 大结果落盘（0 API）
  L1 snip_compact       — 裁掉中间消息（0 API）
  L2 micro_compact      — 旧工具结果变占位符（0 API）
  L4 compact_history    — LLM 全量摘要（1 API）
  应急 reactive_compact — API 报 prompt_too_long 时兜底

进度：✅ 度量 ｜✅ L1 snip ｜✅ L2 micro ｜✅ L3 budget ｜✅ L4 history ｜✅ 应急 reactive
"""
import os
import re
import json
import time
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage, AIMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ═══════════ 压缩阈值（第一步）═══════════
CONTEXT_CHAR_LIMIT = 50_000        # 上下文超过就要压缩
LARGE_RESULT_CHAR_LIMIT = 30_000   # 单条工具结果超过就落盘
MAX_MESSAGES = 50                  # snip 触发的消息条数阈值

# ═══════════ 技能加载（s07 保留）═══════════
def load_skills() -> dict:
    """读 skills/<name>/SKILL.md 的 frontmatter，正文先存着但不注入 prompt"""
    skills = {}
    for d in sorted(Path("skills").iterdir()):
        f = d / "SKILL.md"
        if not (d.is_dir() and f.exists()):
            continue
        content = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
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
_catalog = "\n".join(f"- {n}: {v['description']}" for n, v in SKILLS.items())

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
    """加载某个技能的完整内容。当任务需要某项专业技能时（如 C++ 审查）调用。"""
    if name not in SKILLS:
        return f"没有名为「{name}」的技能。可用技能：{', '.join(SKILLS)}"
    body = SKILLS[name]["body"]
    print(f"      ⤵ [load_skill] 注入技能「{name}」正文（{len(body)} 字符）")
    return body

tools = [run_bash, load_skill]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}


# ═══════════════════════════════════════════════════════════
# 压缩机制 —— 第一步：度量
# ═══════════════════════════════════════════════════════════
def estimate_chars(messages) -> int:
    """估算当前 messages 的字符总量（序列化整条消息，含 tool_calls）

    注意：只算 content 会漏掉 tool_calls（AIMessage 调工具时 content 为空），
         而 tool_calls 也是要发给模型、实打实占上下文的。
    """
    if not messages:
        return 0

    def to_dict(m):
        d = {"content": m.content}
        if getattr(m, "tool_calls", None):
            d["tool_calls"] = m.tool_calls
        if getattr(m, "tool_call_id", None):
            d["tool_call_id"] = m.tool_call_id
        return d

    return len(json.dumps([to_dict(m) for m in messages], default=str, ensure_ascii=False))


# ═══════════════════════════════════════════════════════════
# 压缩机制 —— 第二步：L1 snip_compact（裁中间消息）
# ═══════════════════════════════════════════════════════════
def snip_compact(messages, max_messages: int = MAX_MESSAGES):
    """消息条数超过 max_messages 时：保留头 3 + 尾 (max-3)，中间用 marker 替代。

    ⚠️ 核心难点：不能让 tool_call 和 tool_result 分离！
       判断的是【边界的邻居】，而不是【边界自己】。
    """
    if len(messages) <= max_messages:
        return messages

    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail

    # ① 头部右移：如果【头的最后一条】在调工具 → 它后面的工具结果也要保留
    if head_end > 0 and getattr(messages[head_end - 1], "tool_calls", None):
        while head_end < len(messages) and isinstance(messages[head_end], ToolMessage):
            head_end += 1

    # ② 尾部左移：如果【尾的第一条】是工具结果，且它的"调用"在前一条 → 把调用也带上
    if (tail_start > 0 and tail_start < len(messages)
            and isinstance(messages[tail_start], ToolMessage)
            and getattr(messages[tail_start - 1], "tool_calls", None)):
        tail_start -= 1

    # ③ 保险：调整后头尾交叉 → 不裁
    if head_end >= tail_start:
        return messages

    snipped = tail_start - head_end
    marker = HumanMessage(content=f"[snipped {snipped} messages from conversation middle]")
    return messages[:head_end] + [marker] + messages[tail_start:]

KEEP_RECENT = 3# 保留最新 3 条消息
MIN_COMPACT_SIZE = 120# 最小压缩大小（字符数）
REPLACEMENT = "[Earlier tool result compacted. Re-run if needed.]"# 占位符

def micro_compact(messages, target_chars):
    # ① 入口看【触发线】
    if estimate_chars(messages) <= CONTEXT_CHAR_LIMIT:
        return messages

    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    keep_indices = set(tool_indices[-KEEP_RECENT:])

    new_messages = messages.copy()
    for i, m in enumerate(new_messages):
        if not isinstance(m, ToolMessage) or i in keep_indices:
            continue
        if len(str(m.content)) <= MIN_COMPACT_SIZE:       # ← ③ 小结果跳过（保本线）
            continue
        # ⑤ 占位符要「继承」恢复信息：已落盘的保留路径，其他的说"重跑"
        old_content = str(m.content)
        if "saved to " in old_content:
            archived = old_content.split("saved to ", 1)[1].split("]", 1)[0].strip()
            placeholder = f"[Earlier large tool result archived at {archived}. Read it if needed.]"
        else:
            placeholder = REPLACEMENT
        new_messages[i] = ToolMessage(content=placeholder, tool_call_id=m.tool_call_id)
        if estimate_chars(new_messages) <= target_chars:  # ← ④ 达标就停（早停）
            break
    return new_messages

TOOL_RESULT_BUDGET = 200_000       # 所有工具结果的总字符上限
PREVIEW_CHARS = 2_000              # 落入磁盘后，留在上下文里的预览长度
TOOL_RESULTS_DIR = Path(".task_outputs/tool-results")


def _tool_result_chars(messages) -> int:
    """只量【工具结果】那部分的字符数（口径与 estimate_chars 不同：
    estimate_chars 量整个上下文，这里只量工具结果，因为预算按工具结果定）"""
    return sum(len(str(m.content)) for m in messages if isinstance(m, ToolMessage))


def tool_result_budget(messages):
    """把过大的工具结果【落盘】，上下文只留路径 + 预览（信息不丢）"""
    # ① 总字符没超预算 → 什么都不做（guard clause）
    if _tool_result_chars(messages) <= TOOL_RESULT_BUDGET:
        return messages

    # ② 按内容长度【降序】排（最大的先处理：同一次写盘，收益差十几倍）
    tool_msgs = sorted(
        [(i, m) for i, m in enumerate(messages) if isinstance(m, ToolMessage)],
        key=lambda pair: len(str(pair[1].content)),
        reverse=True,
    )

    new_messages = messages.copy()
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for i, m in tool_msgs:
        content = str(m.content)
        # ③ 不够大的跳过（保本线：小结果落盘，收益抵不上文件管理成本）
        if len(content) <= LARGE_RESULT_CHAR_LIMIT:
            continue

        # ④ 完整内容写盘 —— 「信息不丢」的物理保证
        path = TOOL_RESULTS_DIR / f"{m.tool_call_id}.txt"
        path.write_text(content, encoding="utf-8")

        # ⑤ 上下文里换成：路径 + 预览 + 使用提示（不告诉模型路径，它只会瞎猜）
        preview = content[:PREVIEW_CHARS]
        new_messages[i] = ToolMessage(
            content=(f"[Large tool result saved to {path}]\n"
                     f"[Preview — first {PREVIEW_CHARS} chars]\n{preview}\n"
                     f"[/Preview]\n"
                     f"Re-read {path} if you need the full content."),
            tool_call_id=m.tool_call_id,          # 老规矩：配对不能断
        )
        print(f"      💾 [tool_result_budget] {len(content):,} 字符 → {path.name}")

        # ⑥ 降到预算内就收手（早停）
        if _tool_result_chars(new_messages) <= TOOL_RESULT_BUDGET:
            break

    return new_messages


# ═══════════════════════════════════════════════════════════
# 压缩机制 —— 第五步：L4 compact_history（LLM 全量摘要）
# ═══════════════════════════════════════════════════════════
TRANSCRIPTS_DIR = Path(".transcripts")
MAX_COMPACT_FAILURES = 3        # 熔断阈值：连续失败这么多次就停用
compact_failures = 0            # 失败计数（跨轮次，所以放模块级）

COMPACT_PROMPT = """你正在压缩一段【编码会话】的历史，为后续工作腾出上下文空间。
请产出一份【事实性状态摘要】——不是对话复述，而是"接手的人需要知道的状态"。

必须保留以下信息：
1. **用户目标**：用户最初要求什么（简短的话可以原文引用）
2. **涉及文件**：读/创建/修改过的文件【精确路径】
3. **已做决策**：决定了什么、为什么（尤其要写【被否决的方案】和否决理由）
4. **已完成工作**：已经做完并且验证过的
5. **剩余工作**：还没做的，按顺序列出
6. **用户约束**：用户提过的硬性要求（风格、禁止事项、偏好）
7. **遇到的错误**：失败过什么、怎么解决的

规则：
- 简洁但完整。优先写事实和文件路径，不要叙述过程。
- 不要写"可以重新推导"的工具调用细节。
- **不确定的地方要明确说"不确定"**，不要编。

待压缩的会话历史：
{history}
"""


def write_transcript(messages) -> str:
    """把【完整历史】写到 .transcripts/，返回路径。

    为什么必须在摘要之前做：摘要是"有损压缩"，一旦概括，细节就永久丢了。
    先落一份【人类可读】的原始记录，出问题还能翻出来对比。
    （和 L3「搬家不丢弃」是同一个哲学：能留底就留底。）
    """
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPTS_DIR / f"{stamp}.txt"

    lines = []
    for i, m in enumerate(messages):
        tool_calls = getattr(m, "tool_calls", None)
        calls = f"  → 调用 {[t['name'] for t in tool_calls]}" if tool_calls else ""
        # 每条截断 500 字符：transcript 是【给人看的】，不需要全量（否则文件巨大）
        lines.append(f"[{i}] {type(m).__name__}{calls}\n{str(m.content)[:500]}\n")

    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def fallback_summary(messages) -> str:
    """LLM 摘要失败时的【规则降级】——保住骨架，别让管线崩掉。

    原则：抓两个锚点——最初的请求（目标）＋ 最近几轮（当前状态）。
    """
    parts = ["[Fallback summary — LLM 摘要不可用，规则降级]"]
    if messages:
        parts.append(f"最初请求：{str(messages[0].content)[:200]}")
    parts.append(f"历史消息总数：{len(messages)}")
    parts.append("最近几轮：")
    for m in messages[-3:]:
        parts.append(f"  [{type(m).__name__}] {str(m.content)[:200]}")
    return "\n".join(parts)


def compact_history(messages, active_request=None) -> list:
    """L4：LLM 全量摘要（最后手段，唯一花 API 的一层）。

    前三层都压不动时才轮到它：把【整段历史】总结成一份状态摘要，替换掉全部消息。
    """
    global compact_failures

    # ① 先留底 —— 顺序不能反！摘要之后就没原始记录了
    transcript = write_transcript(messages)
    print(f"      📜 [compact_history] 完整历史留底 → {transcript}")

    # ② 生成摘要（带熔断保护）
    if compact_failures >= MAX_COMPACT_FAILURES:
        # 已熔断：不再尝试 API（否则每次重试都烧钱 + 卡住整条管线）
        summary = fallback_summary(messages)
        print(f"      ⚡ [compact_history] 已熔断（失败 {compact_failures} 次），改用降级摘要")
    else:
        # ⚠️ 喂给 LLM 的历史【也要截断】——否则 prompt 自己就会超限，摘要永远失败
        history_text = "\n".join(
            f"[{type(m).__name__}] {str(m.content)[:1500]}" for m in messages
        )
        try:
            raw = llm.invoke(COMPACT_PROMPT.format(history=history_text)).content
            # content 类型可能是 str 或 list[block]，统一成字符串
            summary = (raw if isinstance(raw, str) else str(raw)).strip()
            print(f"      🧠 [compact_history] LLM 摘要完成（{len(summary)} 字符）")
        except Exception as error:
            compact_failures += 1
            print(f"      ⚠️ [compact_history] 摘要失败"
                  f"（{compact_failures}/{MAX_COMPACT_FAILURES}）：{error}")
            summary = fallback_summary(messages)

    # ③ 拼成一条 [Compacted] 消息 —— 替换【整个】历史
    compacted = (f"[Compacted]\n{summary}\n\n"
                 f"[完整历史留底：{transcript}]")

    # ④ 当前用户请求单独附上（防止被摘要"稀释"掉）
    if active_request:
        compacted += f"\n\n[Active user request]\n{active_request}"

    return [HumanMessage(content=compacted)]


# ═══════════════════════════════════════════════════════════
# 压缩机制 —— 第六步：应急层 reactive_compact（API 报错后抢救）
# ═══════════════════════════════════════════════════════════
KEEP_NEWEST = 5                 # 应急时保住最新几条（"当前工作现场"不能丢）
MAX_REACTIVE_RETRIES = 1        # 只重试一次（防"压缩→还超→再压缩"死循环）
reactive_retries = 0            # 跨轮次计数，所以放模块级

# 各家 API 对"上下文超限"的措辞不同，用关键词匹配比用异常类型更通用
TOO_LONG_MARKERS = (
    "prompt_too_long",
    "context_length_exceeded",
    "maximum context length",
    "too many tokens",
    "exceeds the maximum",
    "reduce the length",
)


def is_prompt_too_long(error) -> bool:
    """判断这个异常是不是"上下文超限"。

    为什么用关键词而不是异常类型：Anthropic / OpenAI / DeepSeek 措辞不同，
    而且 OpenAI 兼容层会把错误塞进普通异常的消息文本里——只能看文本。
    """
    text = str(error).lower()
    return any(marker in text for marker in TOO_LONG_MARKERS)


def reactive_compact(messages, active_request=None) -> list:
    """应急层：API 已经报 prompt_too_long 了，抢救式压缩。

    与 L4 的关键区别：
      L4 = 把【整个历史】换成一条摘要（激进，谁都不保）
      这里 = 【保住最新 KEEP_NEWEST 条】+ 前面摘要（温和，保住当前现场）
    因为是"临死抢救"——模型马上要用的上下文不能丢。
    """
    if len(messages) <= KEEP_NEWEST:
        return list(messages)                   # 太短，压无可压

    split = len(messages) - KEEP_NEWEST

    # ⚠️ 边界调整（和 L1 是同一个坑！）：tail 的第一条若是工具结果，
    #    它的"调用"必须一起带过来，否则配对断裂 → API 又报错 → 白压
    while (split > 0 and isinstance(messages[split], ToolMessage)
           and getattr(messages[split - 1], "tool_calls", None)):
        split -= 1

    old_part = list(messages[:split])
    tail = list(messages[split:])

    # 前面那部分：复用 L4 的"留底 + LLM 摘要"能力
    transcript = write_transcript(old_part)
    history_text = "\n".join(f"[{type(m).__name__}] {str(m.content)[:1500]}" for m in old_part)
    try:
        raw = llm.invoke(COMPACT_PROMPT.format(history=history_text)).content
        summary = (raw if isinstance(raw, str) else str(raw)).strip()
    except Exception as error:
        print(f"      🚑 [reactive_compact] 摘要失败（{error}），改用降级摘要")
        summary = fallback_summary(old_part)

    header = (f"[Reactive compact — 上下文超限后的紧急压缩]\n{summary}\n\n"
              f"[被压缩的旧历史留底：{transcript}]")
    if active_request:
        header += f"\n\n[Active user request]\n{active_request}"

    print(f"      🚑 [reactive_compact] 前 {split} 条 → 摘要，保住最新 {len(tail)} 条")

    return [HumanMessage(content=header)] + tail


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

if __name__ == "__main__":
    messages = [HumanMessage(content=query)]
    round_no = 0
    t0 = time.time()

    while True:
        round_no += 1
        # ── 压缩管线：便宜的先跑，贵的后跑（顺序即设计）──
        messages = tool_result_budget(messages)     # L3：大结果先落盘（信息不丢，且必须最先跑）
        messages = snip_compact(messages)           # L1：裁掉中间消息（管条数）
        if estimate_chars(messages) > CONTEXT_CHAR_LIMIT:                        # 还超？
            messages = micro_compact(messages, int(CONTEXT_CHAR_LIMIT * 0.8))    # L2：旧结果换占位符
        if estimate_chars(messages) > CONTEXT_CHAR_LIMIT:                        # 前三层都压不动？
            messages = compact_history(messages, active_request=query)           # L4：LLM 全量摘要（最后手段）

        print(f"──── 第 {round_no} 轮 ────  上下文: {estimate_chars(messages)} / {CONTEXT_CHAR_LIMIT} 字符"
              f"（{len(messages)} 条消息）")

        convo = [SystemMessage(content=SYSTEM_TMPL.format(catalog=_catalog))] + messages
        try:
            response = llm_with_tools.invoke(convo)
        except Exception as error:
            # 真错误驱动的兜底：估算没超、实际超了 → 抢救式压缩后重试
            if is_prompt_too_long(error) and reactive_retries < MAX_REACTIVE_RETRIES:
                reactive_retries += 1
                messages = reactive_compact(messages, active_request=query)
                continue                    # 压缩完重试本轮（不消耗轮次）
            raise                           # 其他错误 / 重试已用尽 → 抛出去
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
