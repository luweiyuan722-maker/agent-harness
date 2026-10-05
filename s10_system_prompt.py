"""s10: System Prompt —— 运行时组装，不硬编码

课程：learn-claudecode s10
主旨：「prompt 是组装出来的，不是写死的」—— 分段 + 按需拼接 + 缓存。

  分段   PROMPT_SECTIONS 字典（identity / tools / workspace / memory）
  拼接   assemble_system_prompt()：始终 3 段 + 按需 memory 段（空行分隔）
  缓存   get_system_prompt()：json.dumps 做 key，context 不变直接返回缓存
  状态   update_context()：查真实状态（文件是否存在 / 工具是否注册），不猜关键词

s09 的记忆 + 压缩机制保留在下方 —— s10 只重构「system prompt 怎么来」。
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


# ═══════════════════════════════════════════════════════════
# 记忆层（s09）：跨压缩、跨会话的持久记忆
# ═══════════════════════════════════════════════════════════
MEMORY_DIR = Path(".memory")
INDEX_FILE = MEMORY_DIR / "MEMORY.md"

def _slugify(name: str) -> str:
    """把记忆名变成安全的文件名"""
    slug = name.lower()                                  
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", slug).strip("-")
    return slug                


def write_memory_file(name, mem_type, description, body, dedupe: bool = True) -> Path:
    """写一条记忆到 .memory/<slug>.md，然后重建索引。

    dedupe=True：先查重，若和已有记忆是同一件事 → 【更新】那条，不新建文件。
    （不去重的话，每次任务结束都会把同一偏好再记一遍 —— 实测第二次运行就重复了。）
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = MEMORY_DIR / f"{_slugify(name)}.md"

    if dedupe:
        dup = find_duplicate(name, description)
        if dup:
            old = next((f for f in list_memory_files() if f["filename"] == dup), None)
            if old:
                name = old["name"]                  # 沿用旧名字，避免文件名漂移
                path = MEMORY_DIR / dup
                print(f"      ♻️ [memory] 与已有记忆重复 → 更新「{name}」")

    content = (f"---\n"
               f"name: {name}\n"
               f"description: {description}\n"
               f"type: {mem_type}\n"
               f"---\n\n"
               f"{body}\n")
    path.write_text(content, encoding="utf-8")
    _rebuild_index()
    return path

def _rebuild_index() -> None:
    """扫描 .memory/*.md，重建 MEMORY.md（一行一个链接）"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = []# 记忆�链接列表
    for m in list_memory_files():
        lines.append(f"- [{m['name']}]({m['filename']}) — {m['description']}")
    INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")# 写入索引文件
    
def _parse_frontmatter(text: str) -> dict:
    """从记忆文件文本里解析出 frontmatter，返回 {'name':..., 'description':..., 'type':...}"""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)   # ① 抓 --- 之间的内容
    if not m:
        return {}
    front = m.group(1)                                   # ② 抓到的 frontmatter 原始文本
    result = {}
    for key in ("name", "description", "type"):          # ③ 三个字段逐个找
        hit = re.search(rf"^{key}:\s*(.+)$", front, re.MULTILINE)
        if hit:
            result[key] = hit.group(1).strip()
    return result


def list_memory_files() -> list[dict]:
    """列出所有记忆的元数据 {filename, name, description, type}（解析 frontmatter）"""
    if not MEMORY_DIR.exists():
        return []
    metas = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == INDEX_FILE.name:
            continue
        text = f.read_text(encoding="utf-8")
        front = _parse_frontmatter(text)
        metas.append({
            "filename": f.name,
            "name": front.get("name", f.stem),           # ⑥ 没 name 就用文件名兜底
            "description": front.get("description", ""),
            "type": front.get("type", ""),
        })
    return metas

def build_memory_catalog() -> str:
    """给 SYSTEM prompt 用的记忆清单（一行一条，很轻）"""
    # 你的代码...
    lines = []
    for m in list_memory_files():
        lines.append(f"- {m['name']} ({m['type']}): {m['description']}")
    if not lines:
        return "暂无记忆"
    return "\n".join(lines)

def read_memory_body(filename: str) -> str:
    """读一条记忆的正文（去掉 frontmatter）"""
    path = MEMORY_DIR / filename
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _extract_keywords(text: str) -> set:
    """从文本提取关键词：英文/数字词 + 中文 bigram（2 字滑动窗口）

    中文没有空格分词，用 2 字滑动窗口近似"词"：
      "内存管理用" → {"内存", "存管", "管理", "理用"}
      "内存泄漏"   → {"内存", "存泄", "泄漏"}
    交集 {"内存"} → 能匹配上（整段子串匹配做不到这点）。
    """
    text = text.lower()
    keys = set(re.findall(r"[a-z0-9]{3,}", text))      # 英文/数字：≥3 字符
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):  # 每段连续中文
        for i in range(len(seg) - 1):
            keys.add(seg[i:i+2])                       # 2 字滑动窗口
    return keys


def _keyword_fallback(messages, files, max_items) -> list:
    """side-query 失败时的降级：关键词匹配 name+description

    为什么是"兜底"：如果连 LLM 都挂了（网络/超时/返回格式错），
    就不能再用 LLM 筛选 —— 退回纯本地字符串比对，永不抛异常。
    宁可粗糙多选几条，也不能让模型完全看不到记忆。
    """
    recent_keys = _extract_keywords(" ".join(str(m.content) for m in messages[-3:]))
    scored = []
    for f in files:
        hay_keys = _extract_keywords(f"{f['name']} {f['description']}")
        overlap = len(hay_keys & recent_keys)   # 集合交集大小 = 命中几个词
        if overlap > 0:
            scored.append((overlap, f["filename"]))
    scored.sort(reverse=True)                   # 分数高的在前
    return [fn for _, fn in scored[:max_items]]


def select_relevant_memories(messages, max_items: int = 5) -> list:
    """用一次轻量 LLM 调用筛出相关记忆的 filename 列表"""
    files = list_memory_files()
    if not files:
        return []
    catalog = "\n".join(                      
        f"{i}: {f['name']} — {f['description']}"
        for i, f in enumerate(files)
    )
    recent = "\n".join(                       
        f"[{type(m).__name__}] {str(m.content)[:300]}"
        for m in messages[-3:]
    )

    prompt = (                                
        f"从记忆目录中选出与最近对话相关的，最多 {max_items} 条。\n"
        f"只返回 JSON 数组（如 [0, 2]），不要解释。都不相关就返回 []。\n\n"
        f"最近对话：\n{recent}\n\n记忆目录：\n{catalog}"
    )
    try:
        raw = llm.invoke(prompt).content
        text = raw if isinstance(raw, str) else str(raw)
        hit = re.search(r"\[.*?\]", text, re.DOTALL)
        indices = json.loads(hit.group()) if hit else []
    except Exception:
        # side-query 挂了 → 降级到关键词匹配（不依赖 LLM，永不抛异常）
        print("      🧠 [memory] side-query 失败 → 关键词降级")
        return _keyword_fallback(messages, files, max_items)

    picked = [
        files[i]["filename"]
        for i in indices
        if isinstance(i, int) and 0 <= i < len(files)
    ]
    return picked[:max_items]

def build_memory_injection(messages) -> str:
    """把选中的记忆正文拼成一块，临时注入 user turn"""
    picked = select_relevant_memories(messages)
    if not picked: 
        return ""
    parts = ["<相关记忆>"]
    for fn in picked: 
        body = read_memory_body(fn)
        if body:
            parts.append(f"### {fn}\n{body}")
    parts.append("</相关记忆>")
    return "\n".join(parts)              


# ─────────── 写入层：任务结束后提取记忆（模块 5）───────────
MEMORY_TYPES = ("user", "feedback", "project", "reference")

EXTRACT_PROMPT = """你在整理一段刚结束的编码会话，从中提取【值得跨会话保留】的事实。

只提取这四类（宁缺勿滥，没有就返回空数组 []）：
- user：用户是谁 / 稳定的偏好（如"习惯用 tab 缩进"）
- feedback：用户对做事方式的明确要求（如"别 mock 数据库"）
- project：项目的稳定背景（如"auth 重写是合规驱动的"）
- reference：东西在哪找（如"pipeline bug 记在 Linear INGEST"）

不要提取：一次性的任务细节、临时状态、可以从代码里直接看出来的东西。

每条记忆的格式：
{"name": "短横线命名，如 user-preference-tabs",
 "type": "user|feedback|project|reference",
 "description": "一行摘要（会进索引）",
 "body": "具体内容，必须包含 **Why:**（为什么）和 **How to apply:**（怎么应用）两行"}

只返回 JSON 数组，不要任何解释。

会话内容：
{conversation}
"""


def extract_memories(messages) -> list:
    """任务结束时提取记忆（只在"模型停止 且 无 tool_calls"之后调用）

    为什么不在每轮提取：每轮都提取会 ① 频繁调 API ② 记下"半成品想法"
    （用户可能下一句就改主意了）。任务结束，这轮对话才是完整、稳定的。
    """
    if not messages:
        return []
    conversation = "\n".join(
        f"[{type(m).__name__}] {str(m.content)[:800]}" for m in messages)
    try:
        # ⚠️ 用 replace 而非 format：prompt 里有 JSON 示例 {"name": ...}，
        #    花括号会被 format 当占位符 → KeyError。replace 不解析花括号。
        prompt = EXTRACT_PROMPT.replace("{conversation}", conversation)
        raw = llm.invoke(prompt).content
        text = raw if isinstance(raw, str) else str(raw)
        hit = re.search(r"\[.*?\]", text, re.DOTALL)
        items = json.loads(hit.group()) if hit else []
    except Exception as error:
        print(f"      ⚠️ [memory] 提取失败：{error}")
        return []

    valid = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue                                # 不是字典或没 name → 跳过
        if it.get("type") not in MEMORY_TYPES:
            continue                                # 类型白名单，防 LLM 乱造
        valid.append({
            "name": it["name"],
            "mem_type": it["type"],
            "description": it.get("description", ""),
            "body": it.get("body", ""),
        })
    return valid


# ─────────── 去重层：写入前查重（模块 6）───────────
def find_duplicate(name: str, description: str) -> str:
    """查重：新记忆是不是已有记忆的重复？返回已有记忆的 filename，没有则 ""。

    为什么用 LLM 不用字符串比对：同一件事会有不同措辞 ——
      "用户写 C++ 用 4 个空格缩进"  vs  "用户 C++ 习惯 4 空格，不用 tab"
    字符串相似度会漏判，LLM 能认出"这是同一件事"。
    """
    files = list_memory_files()
    if not files:
        return ""
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))
    prompt = (f"新记忆：{name} — {description}\n\n"
              f"已有记忆：\n{catalog}\n\n"
              f"这条新记忆与已有记忆中的某一条是【同一件事】吗？"
              f"如果是，只返回那个索引数字；如果不是，只返回 -1。不要任何解释。")
    try:
        raw = llm.invoke(prompt).content
        text = raw if isinstance(raw, str) else str(raw)
        hit = re.search(r"-?\d+", text)     # 抓数字（可能带负号 -1）
        idx = int(hit.group()) if hit else -1
        if 0 <= idx < len(files):
            return files[idx]["filename"]
    except Exception:
        pass
    return ""


# ═══════════ Agent Loop ═══════════
SYSTEM_TMPL = """你是任务执行助手。

【记忆索引】（这是长期记忆的目录；完整内容会在相关时自动注入到对话里）
{memory_index}

【可用技能】（需要时用 load_skill 加载完整内容）
{catalog}

【规则】
1. 如果任务匹配某个技能，必须先 load_skill 加载它，再严格按它的清单干活
2. 不匹配任何技能的任务，直接自己完成
3. 【记忆索引】里记录着这个用户的稳定偏好和项目背景 —— 回答时必须遵守，别问已知的事
"""

WORKDIR = Path.cwd()
PROMPT_SECTIONS = {
    "identity":  "You are a coding agent. Act, don't explain.",
    "tools":     "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory":    "Relevant memories are injected below when available.",
}

def assemble_system_prompt(context: dict) -> str:
    """按需拼接：始终加载的 3 段 + 按需加载的 memory 段"""
    sections = []
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(f"Available tools: {', '.join(context['enabled_tools'])}")
    sections.append(PROMPT_SECTIONS["workspace"])
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key = None
_last_prompt = None

def get_system_prompt(context: dict) -> str:
    """context 没变就返回缓存，变了就重新拼接"""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt

def update_context(context: dict) -> dict:
    """根据真实状态（文件是否存在、工具是否注册）更新 context"""
    memories = ""
    if INDEX_FILE.exists():
        content = INDEX_FILE.read_text(encoding="utf-8").strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOLS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


query = ("帮我审查这段 C++ 代码，找出所有问题：\n\n"
         "void process(int n) {\n"
         "    int* p = new int(n);\n"
         "    if (*p > 3) return;\n"
         "    delete p;\n"
         "}\n\n"
         "另外请记住：我写 C++ 习惯用 4 个空格缩进、不用 tab，"
         "以后给我代码示例请保持这个风格。")

if __name__ == "__main__":
    messages = [HumanMessage(content=query)]

    # ── 记忆加载（路径二）：选出相关记忆，临时注入当前 user turn ──
    print("──── 记忆加载 ────")
    print(f"  索引（常驻 SYSTEM）：{len(list_memory_files())} 条记忆")
    memory_block = build_memory_injection(messages)

    round_no = 0
    t0 = time.time()
    context = update_context({})   # 循环前先查一次真实状态（否则第一轮 context['enabled_tools'] 是空 → KeyError）
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

        # SYSTEM 带【记忆索引】（路径一：稳定 → prompt cache 友好）
        system = get_system_prompt(context)
        convo: list = [SystemMessage(content=system)] + messages
        if memory_block:
            convo.append(HumanMessage(content=memory_block))   # 路径二：临时注入（不进 messages）
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

    # ═══════ 任务结束：提取记忆（s09 的核心写入时机）═══════
    print("\n══════ 记忆提取 ══════")
    found = extract_memories(messages)
    if not found:
        print("  （本轮没有值得跨会话保留的事实）")
    for item in found:
        path = write_memory_file(item["name"], item["mem_type"],
                                 item["description"], item["body"])
        print(f"  💾 记住「{path.stem}」（{item['mem_type']}）")
    if found:
        print(f"\n  索引已重建 → {INDEX_FILE}：")
        print(build_memory_catalog())
    context = update_context(context)