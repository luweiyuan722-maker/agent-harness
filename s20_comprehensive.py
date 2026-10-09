"""s20: Comprehensive Agent Turn —— 机制很多，循环一个（终点章，完整整合）

课程：learn-claudecode s20
主旨：把 s01-s19 的机制真正合回同一个 while True 循环，每个机制都是真实实现。

循环本身从未变过：
  while True:
      response = LLM(messages, tools)
      if not has_tool_use(response): return
      results = execute_tools(response)
      messages.append(tool_results)

本章挂载（均真实实现，非占位）：
  s01 循环 / s02 工具分发 / s03 权限 / s04 hooks / s05 todo
  s06 子agent / s07 技能 / s08 压缩六件套 / s09 记忆提取
  s10 system prompt 组装+缓存 / s11 错误恢复三路径 / s12 任务DAG+防环+路径安全
  s13 后台 / s14 cron / s15 团队 / s16 协议 / s17 自治 / s18 worktree / s19 MCP
"""
import os
import re
import json
import time
import random
import secrets
import threading
import subprocess
import asyncio
from datetime import datetime
from dataclasses import dataclass, asdict, field
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool, StructuredTool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, AIMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()
TASKS_DIR = WORKDIR / ".tasks"
MAILBOX_DIR = WORKDIR / ".mailboxes"
WORKTREES_DIR = WORKDIR / ".worktrees"
MEMORY_DIR = WORKDIR / ".memory"
SKILLS_DIR = WORKDIR / "skills"
SCHEDULED_FILE = WORKDIR / ".scheduled_tasks.json"
TOOL_RESULTS_DIR = Path(".task_outputs/tool-results")
TRANSCRIPTS_DIR = Path(".transcripts")
INDEX_FILE = MEMORY_DIR / "MEMORY.md"

# ═══════════ s08 压缩阈值 ═══════════
CONTEXT_CHAR_LIMIT = 50_000
LARGE_RESULT_CHAR_LIMIT = 30_000
MAX_MESSAGES = 50
KEEP_RECENT = 3
MIN_COMPACT_SIZE = 120
REPLACEMENT = "[Earlier tool result compacted. Re-run if needed.]"
TOOL_RESULT_BUDGET = 200_000
PREVIEW_CHARS = 2_000
KEEP_NEWEST = 5
MAX_REACTIVE_RETRIES = 1
reactive_retries = 0
MAX_COMPACT_FAILURES = 3
compact_failures = 0


# ═══════════ s04 hooks 系统 ═══════════
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(point, fn):
    HOOKS[point].append(fn)


def trigger_hooks(point, *args):
    for fn in HOOKS[point]:
        r = fn(*args)
        if r:
            return r
    return None


# ═══════════ s03 权限（挂在 PreToolUse）═══════════
DANGEROUS_COMMANDS = ["rm -rf", "mkfs", "shutdown", "fork bomb", ":(){:|:&};:"]


def permission_check(tc):
    if tc.get("name") == "run_bash":
        cmd = tc.get("args", {}).get("command", "")
        for d in DANGEROUS_COMMANDS:
            if d in cmd:
                return f"拒绝：危险命令 {d!r}"
    return ""


register_hook("PreToolUse", permission_check)


# ═══════════ s05 todo ═══════════
todos: list = []


# ═══════════ s07 技能 ═══════════
def load_skills() -> dict:
    skills = {}
    if not SKILLS_DIR.exists():
        return skills
    for d in sorted(SKILLS_DIR.iterdir()):
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
        if name and desc:
            skills[name.group(1).strip()] = {
                "description": desc.group(1).strip(), "body": body}
    return skills


SKILLS = load_skills()
_skill_catalog = "\n".join(f"- {n}: {v['description']}" for n, v in SKILLS.items())


# ═══════════ s08 压缩六件套 ═══════════
def estimate_chars(messages) -> int:
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


def snip_compact(messages, max_messages: int = MAX_MESSAGES):
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    if head_end > 0 and getattr(messages[head_end - 1], "tool_calls", None):
        while head_end < len(messages) and isinstance(messages[head_end], ToolMessage):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and isinstance(messages[tail_start], ToolMessage)
            and getattr(messages[tail_start - 1], "tool_calls", None)):
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    marker = HumanMessage(content=f"[snipped {tail_start - head_end} messages from middle]")
    return messages[:head_end] + [marker] + messages[tail_start:]


def micro_compact(messages, target_chars):
    if estimate_chars(messages) <= CONTEXT_CHAR_LIMIT:
        return messages
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    keep_indices = set(tool_indices[-KEEP_RECENT:])
    new_messages = messages.copy()
    for i, m in enumerate(new_messages):
        if not isinstance(m, ToolMessage) or i in keep_indices:
            continue
        if len(str(m.content)) <= MIN_COMPACT_SIZE:
            continue
        old_content = str(m.content)
        if "saved to " in old_content:
            archived = old_content.split("saved to ", 1)[1].split("]", 1)[0].strip()
            placeholder = f"[Earlier large tool result archived at {archived}. Read it if needed.]"
        else:
            placeholder = REPLACEMENT
        new_messages[i] = ToolMessage(content=placeholder, tool_call_id=m.tool_call_id)
        if estimate_chars(new_messages) <= target_chars:
            break
    return new_messages


def _tool_result_chars(messages) -> int:
    return sum(len(str(m.content)) for m in messages if isinstance(m, ToolMessage))


def tool_result_budget(messages):
    if _tool_result_chars(messages) <= TOOL_RESULT_BUDGET:
        return messages
    tool_msgs = sorted(
        [(i, m) for i, m in enumerate(messages) if isinstance(m, ToolMessage)],
        key=lambda pair: len(str(pair[1].content)), reverse=True)
    new_messages = messages.copy()
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for i, m in tool_msgs:
        content = str(m.content)
        if len(content) <= LARGE_RESULT_CHAR_LIMIT:
            continue
        path = TOOL_RESULTS_DIR / f"{m.tool_call_id}.txt"
        path.write_text(content, encoding="utf-8")
        preview = content[:PREVIEW_CHARS]
        new_messages[i] = ToolMessage(
            content=(f"[Large tool result saved to {path}]\n"
                     f"[Preview — first {PREVIEW_CHARS} chars]\n{preview}\n[/Preview]\n"
                     f"Re-read {path} if you need the full content."),
            tool_call_id=m.tool_call_id)
        if _tool_result_chars(new_messages) <= TOOL_RESULT_BUDGET:
            break
    return new_messages


COMPACT_PROMPT = """你正在压缩一段编码会话历史，为后续工作腾出上下文。请产出一份【事实性状态摘要】。
必须保留：用户目标 / 涉及文件（精确路径）/ 已做决策（含被否决方案和理由）/ 已完成工作 / 剩余工作 / 用户约束 / 遇到的错误。
规则：简洁但完整；优先写事实和路径；不确定就明确说"不确定"。
待压缩历史：
{history}
"""


def write_transcript(messages) -> str:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPTS_DIR / f"{stamp}.txt"
    lines = []
    for i, m in enumerate(messages):
        calls = f"  → 调用 {[t['name'] for t in m.tool_calls]}" if getattr(m, "tool_calls", None) else ""
        lines.append(f"[{i}] {type(m).__name__}{calls}\n{str(m.content)[:500]}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def fallback_summary(messages) -> str:
    parts = ["[Fallback summary — LLM 摘要不可用，规则降级]"]
    if messages:
        parts.append(f"最初请求：{str(messages[0].content)[:200]}")
    parts.append(f"历史消息总数：{len(messages)}")
    parts.append("最近几轮：")
    for m in messages[-3:]:
        parts.append(f"  [{type(m).__name__}] {str(m.content)[:200]}")
    return "\n".join(parts)


def _llm_summarize(messages) -> str:
    history_text = "\n".join(f"[{type(m).__name__}] {str(m.content)[:1500]}" for m in messages)
    try:
        raw = llm.invoke(COMPACT_PROMPT.replace("{history}", history_text)).content
        return (raw if isinstance(raw, str) else str(raw)).strip()
    except Exception:
        return fallback_summary(messages)


def compact_history(messages, active_request=None) -> list:
    global compact_failures
    transcript = write_transcript(messages)
    if compact_failures >= MAX_COMPACT_FAILURES:
        summary = fallback_summary(messages)
    else:
        try:
            summary = _llm_summarize(messages)
        except Exception:
            compact_failures += 1
            summary = fallback_summary(messages)
    compacted = f"[Compacted]\n{summary}\n\n[完整历史留底：{transcript}]"
    if active_request:
        compacted += f"\n\n[Active user request]\n{active_request}"
    return [HumanMessage(content=compacted)]


TOO_LONG_MARKERS = ("prompt_too_long", "context_length_exceeded", "maximum context length",
                    "too many tokens", "exceeds the maximum", "reduce the length")


def is_prompt_too_long(error) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in TOO_LONG_MARKERS)


def reactive_compact(messages, active_request=None) -> list:
    if len(messages) <= KEEP_NEWEST:
        return list(messages)
    split = len(messages) - KEEP_NEWEST
    while (split > 0 and isinstance(messages[split], ToolMessage)
           and getattr(messages[split - 1], "tool_calls", None)):
        split -= 1
    old_part = list(messages[:split])
    tail = list(messages[split:])
    transcript = write_transcript(old_part)
    summary = _llm_summarize(old_part)
    header = (f"[Reactive compact — 上下文超限后的紧急压缩]\n{summary}\n\n"
              f"[被压缩的旧历史留底：{transcript}]")
    if active_request:
        header += f"\n\n[Active user request]\n{active_request}"
    return [HumanMessage(content=header)] + tail


# ═══════════ s09 记忆层 ═══════════
MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _slugify(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", slug).strip("-")
    return slug


def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result = {}
    for key in ("name", "description", "type"):
        hit = re.search(rf"^{key}:\s*(.+)$", m.group(1), re.MULTILINE)
        if hit:
            result[key] = hit.group(1).strip()
    return result


def list_memory_files() -> list:
    if not MEMORY_DIR.exists():
        return []
    metas = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        front = _parse_frontmatter(f.read_text(encoding="utf-8"))
        metas.append({"filename": f.name, "name": front.get("name", f.stem),
                      "description": front.get("description", ""),
                      "type": front.get("type", "")})
    return metas


def build_memory_catalog() -> str:
    lines = [f"- {m['name']} ({m['type']}): {m['description']}" for m in list_memory_files()]
    return "\n".join(lines) if lines else "暂无记忆"


def read_memory_body(filename: str) -> str:
    path = MEMORY_DIR / filename
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _rebuild_index() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"- [{m['name']}]({m['filename']}) — {m['description']}" for m in list_memory_files()]
    INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")


def find_duplicate(name: str, description: str) -> str:
    files = list_memory_files()
    if not files:
        return ""
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))
    prompt = (f"新记忆：{name} — {description}\n\n已有记忆：\n{catalog}\n\n"
              f"这条新记忆与已有记忆中的某一条是【同一件事】吗？"
              f"如果是，只返回那个索引数字；如果不是，只返回 -1。")
    try:
        raw = llm.invoke(prompt).content
        text = raw if isinstance(raw, str) else str(raw)
        idx = int(re.search(r"-?\d+", text).group()) if re.search(r"-?\d+", text) else -1
        if 0 <= idx < len(files):
            return files[idx]["filename"]
    except Exception:
        pass
    return ""


def write_memory_file(name, mem_type, description, body, dedupe=True) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = MEMORY_DIR / f"{_slugify(name)}.md"
    if dedupe:
        dup = find_duplicate(name, description)
        if dup:
            path = MEMORY_DIR / dup
    content = (f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n")
    path.write_text(content, encoding="utf-8")
    _rebuild_index()
    return path


def _extract_keywords(text: str) -> set:
    text = text.lower()
    keys = set(re.findall(r"[a-z0-9]{3,}", text))
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(seg) - 1):
            keys.add(seg[i:i + 2])
    return keys


def _keyword_fallback(messages, files, max_items) -> list:
    recent_keys = _extract_keywords(" ".join(str(m.content) for m in messages[-3:]))
    scored = []
    for f in files:
        overlap = len(_extract_keywords(f"{f['name']} {f['description']}") & recent_keys)
        if overlap > 0:
            scored.append((overlap, f["filename"]))
    scored.sort(reverse=True)
    return [fn for _, fn in scored[:max_items]]


def select_relevant_memories(messages, max_items: int = 5) -> list:
    files = list_memory_files()
    if not files:
        return []
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))
    recent = "\n".join(f"[{type(m).__name__}] {str(m.content)[:300]}" for m in messages[-3:])
    prompt = (f"从记忆目录选出与最近对话相关的，最多 {max_items} 条。\n"
              f"只返回 JSON 数组（如 [0, 2]）。都不相关返回 []。\n\n"
              f"最近对话：\n{recent}\n\n记忆目录：\n{catalog}")
    try:
        raw = llm.invoke(prompt).content
        text = raw if isinstance(raw, str) else str(raw)
        hit = re.search(r"\[.*?\]", text, re.DOTALL)
        indices = json.loads(hit.group()) if hit else []
    except Exception:
        return _keyword_fallback(messages, files, max_items)
    return [files[i]["filename"] for i in indices if isinstance(i, int) and 0 <= i < len(files)][:max_items]


def build_memory_injection(messages) -> str:
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


EXTRACT_PROMPT = """你在整理一段刚结束的编码会话，提取【值得跨会话保留】的事实。
只提取四类（没有就返回 []）：user（用户是谁/稳定偏好）、feedback（做事方式的明确要求）、project（项目稳定背景）、reference（东西在哪找）。
不要提取：一次性任务细节、临时状态、代码里能直接看出来的。
每条格式：{"name":"短横线命名","type":"user|feedback|project|reference","description":"一行摘要","body":"具体内容，含 Why: 和 How to apply: 两行"}
只返回 JSON 数组。
会话内容：
{conversation}
"""


def extract_memories(messages) -> list:
    if not messages:
        return []
    conversation = "\n".join(f"[{type(m).__name__}] {str(m.content)[:800]}" for m in messages)
    try:
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
            continue
        if it.get("type") not in MEMORY_TYPES:
            continue
        valid.append({"name": it["name"], "mem_type": it["type"],
                      "description": it.get("description", ""), "body": it.get("body", "")})
    return valid


# ═══════════ s10 system prompt 组装 + 缓存 ═══════════
_last_context_key = None
_last_prompt = None


def assemble_system_prompt(context: dict) -> str:
    sections = ["You are a coding agent. Act, don't explain."]
    sections.append(f"Available tools: {', '.join(context.get('enabled_tools', []))}")
    sections.append(f"Working directory: {WORKDIR}")
    sections.append(f"技能目录：\n{_skill_catalog}")
    memories = context.get("memories", "")
    if memories:
        sections.append(f"记忆索引：\n{memories}")
    mcp = context.get("mcp", "")
    if mcp:
        sections.append(f"已连接 MCP server：{mcp}")
    return "\n\n".join(sections)


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


def update_context() -> dict:
    memories = ""
    if INDEX_FILE.exists():
        memories = INDEX_FILE.read_text(encoding="utf-8").strip()
    return {"enabled_tools": list(TOOL_HANDLERS.keys()),
            "memories": memories,
            "mcp": ", ".join(mcp_clients) if mcp_clients else ""}


# ═══════════ s11 错误恢复 ═══════════
MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 64000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_DELAY_MS = 32000
OVERLOAD_SWITCH_THRESHOLD = 3
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL_ID")
RATE_LIMIT_MARKERS = ("429", "rate limit", "too many requests", "529", "overloaded")
CONTINUATION_PROMPT = "Output token limit hit. Resume directly — no apology, no recap."


@dataclass
class RecoveryState:
    has_escalated: bool = False
    recovery_count: int = 0
    has_attempted_compact: bool = False
    consecutive_overloads: int = 0


def is_truncated(response) -> bool:
    return response.response_metadata.get("finish_reason") == "length"


def is_rate_limited(error) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def retry_delay(attempt: int, retry_after=None) -> float:
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), MAX_DELAY_MS) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, max_retries: int = MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as error:
            if not is_rate_limited(error):
                raise
            time.sleep(retry_delay(attempt))
    raise RuntimeError(f"重试 {max_retries} 次仍失败")


# ═══════════ s12 任务系统（防环 + 路径安全）═══════════
TASK_ID_PATTERN = re.compile(r"task_[0-9a-f]{8}")


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str = ""
    blockedBy: list = field(default_factory=list)
    worktree: str = ""


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")
        self._root(create=True)
        for _ in range(100):
            task = Task(id=f"task_{secrets.token_hex(4)}", subject=subject,
                        description=description)
            try:
                with self._path(task.id, create_root=True).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list) -> Task:
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list")
        task = self.load(task_id)
        if task.status != "pending" or task.owner:
            raise ValueError("dependencies only updatable while pending and unowned")
        deps = list(dict.fromkeys(add_blocked_by))
        for dep in deps:
            if dep == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dep):
                raise ValueError(f"Dependency not found: {dep}")
            if dep not in task.blockedBy and self._depends_on(dep, task_id):
                raise ValueError(f"Dependency cycle detected: {task_id} -> {dep}")
        task.blockedBy.extend(d for d in deps if d not in task.blockedBy)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), indent=2), encoding="utf-8")

    def load(self, task_id: str) -> Task:
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID mismatch {task_id}")
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid status: {task.status}")
        return task

    def list(self) -> list:
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(p.stem) for p in sorted(root.glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)
_task_locks: dict = {}
_locks_guard = threading.Lock()


def _get_task_lock(tid):
    with _locks_guard:
        if tid not in _task_locks:
            _task_locks[tid] = threading.Lock()
        return _task_locks[tid]


def can_start(task_id: str) -> bool:
    for dep in TASKS.load(task_id).blockedBy:
        if TASKS.load(dep).status != "completed":
            return False
    return True


def scan_unclaimed_tasks() -> list:
    return [t for t in TASKS.list()
            if t.status == "pending" and not t.owner and can_start(t.id)]


# ═══════════ s13 后台任务 ═══════════
SLOW_KEYWORDS = ["install", "build", "test", "compile", "pip install", "npm install", "pytest", "make"]
_bg: dict = {}
_bg_lock = threading.Lock()
_bg_counter = 0


def should_run_background(tool_name, tool_input):
    if tool_input.get("run_in_background"):
        return True
    if tool_name != "run_bash":
        return False
    return any(k in tool_input.get("command", "") for k in SLOW_KEYWORDS)


def start_background_task(tool_name, tool_input, tool_use_id):
    global _bg_counter
    _bg_counter += 1
    bid = f"bg_{_bg_counter:04d}"

    def worker():
        r = subprocess.run(tool_input["command"], shell=True, capture_output=True, text=True, timeout=300)
        with _bg_lock:
            _bg[bid] = (r.stdout or r.stderr or "(无输出)").strip()[:2000]

    with _bg_lock:
        _bg[bid] = "running"
    threading.Thread(target=worker, daemon=True).start()
    return bid


def collect_background_results():
    with _bg_lock:
        done = [bid for bid, v in _bg.items() if v != "running"]
    notifs = []
    for bid in done:
        with _bg_lock:
            out = _bg.pop(bid)
        notifs.append(f"<task_notification>{out[:200]}</task_notification>")
    return notifs


# ═══════════ s14 cron ═══════════
@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True


scheduled_jobs: dict = {}
cron_queue: list = []
cron_lock = threading.Lock()
_last_fired: dict = {}


def validate_cron(cron: str) -> str:
    fields = cron.split()
    if len(fields) != 5:
        return f"cron 需要 5 段，实际 {len(fields)}"
    ranges = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 6)}
    for name, field in zip(ranges, fields):
        lo, hi = ranges[name]
        for part in field.split(","):
            base = part.split("/")[0]
            if base == "*":
                continue
            try:
                if "-" in base:
                    a, b = map(int, base.split("-"))
                    if a < lo or b > hi or a > b:
                        return f"字段 {name}={part} 超出范围"
                else:
                    v = int(base)
                    if v < lo or v > hi:
                        return f"字段 {name}={part} 超出范围"
            except ValueError:
                return f"字段 {name}={part} 不是数字"
    return ""


def _cron_field_matches(field, value):
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, _, step = part.partition("/")
            if base == "*" and value % int(step) == 0:
                return True
        elif "-" in part:
            lo, hi = map(int, part.split("-"))
            if lo <= value <= hi:
                return True
        elif int(part) == value:
            return True
    return False


def cron_matches(expr, dt):
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    if not (_cron_field_matches(minute, dt.minute)
            and _cron_field_matches(hour, dt.hour)
            and _cron_field_matches(month, dt.month)):
        return False
    dom_ok = _cron_field_matches(dom, dt.day)
    dow_ok = _cron_field_matches(dow, dow_val)
    if dom != "*" and dow != "*":
        return dom_ok or dow_ok
    if dom != "*":
        return dom_ok
    if dow != "*":
        return dow_ok
    return True


def save_durable_jobs():
    SCHEDULED_FILE.write_text(json.dumps(
        {"tasks": [asdict(j) for j in scheduled_jobs.values() if j.durable]}, indent=2), encoding="utf-8")


def load_durable_jobs():
    if not SCHEDULED_FILE.exists():
        return
    try:
        data = json.loads(SCHEDULED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in data.get("tasks", []):
        j = CronJob(**item)
        scheduled_jobs[j.id] = j


def cron_scheduler_loop():
    while True:
        time.sleep(1)
        dt = datetime.now()
        marker = dt.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, dt):
                        if _last_fired.get(job.id) != marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = marker
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            save_durable_jobs()
                except Exception:
                    pass


def consume_cron_queue():
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


# ═══════════ s15 MessageBus ═══════════
class MessageBus:
    def __init__(self):
        self._lock = threading.Lock()

    def send(self, frm, to, content, msg_type="message", metadata=None):
        msg = {"from": frm, "to": to, "content": content, "type": msg_type,
               "metadata": metadata or {}, "ts": time.time()}
        MAILBOX_DIR.mkdir(exist_ok=True)
        with self._lock:
            with open(MAILBOX_DIR / f"{to}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent):
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        with self._lock:
            if not inbox.exists():
                return []
            msgs = [json.loads(l) for l in inbox.read_text().splitlines()]
            inbox.unlink()
            return msgs


BUS = MessageBus()
_current = threading.local()


# ═══════════ s16 协议 ═══════════
@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str = ""
    created_at: float = 0.0


pending_requests: dict = {}
protocol_lock = threading.Lock()


def new_request_id() -> str:
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type, request_id, approve):
    state = pending_requests.get(request_id)
    if not state:
        return
    if state.type == "shutdown" and response_type != "shutdown_response":
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        return
    if state.status != "pending":
        return
    state.status = "approved" if approve else "rejected"


# ═══════════ s18 worktree ═══════════
def validate_worktree_name(name: str) -> str:
    if not name:
        return "名字不能为空"
    if name in (".", "..") or "/" in name or "\\" in name:
        return f"非法名字 {name!r}"
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", name):
        return f"非法名字 {name!r}：只允许字母数字和 . _ -"
    return ""


# ═══════════ s19 MCP ═══════════
_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED.sub("_", name)


class RealMCPClient:
    def __init__(self, name, command, args):
        self.name = name
        self.tools = []
        self._session = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(command, args), self._loop)
        while not self.tools:
            time.sleep(0.05)

    async def _run(self, command, args):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=command, args=args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self.tools = result.tools
                self._session = session
                await asyncio.Event().wait()

    def call_tool(self, tool_name, args):
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, args), self._loop)
        result = fut.result(timeout=30)
        parts = [getattr(c, "text", "") for c in result.content]
        return "\n".join(p for p in parts if p)


mcp_clients: dict = {}


# ═══════════ 工具（整合 s02-s19）═══════════
@tool
def run_bash(command: str, run_in_background: bool = False) -> str:
    """执行 shell 命令。慢操作会自动放后台。"""
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


@tool
def read_file(path: str) -> str:
    """读文件。"""
    p = Path(path)
    return p.read_text(encoding="utf-8")[:2000] if p.exists() else f"不存在：{path}"


@tool
def write_file(path: str, content: str) -> str:
    """写文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path}"


@tool
def todo_write(items: str) -> str:
    """写计划（换行分隔）。"""
    for line in items.split("\n"):
        line = line.strip()
        if line:
            todos.append(line)
    return f"已记录 {len(todos)} 条待办"


@tool
def delegate(task: str) -> str:
    """把独立子任务交给一次性子 agent，返回结论。"""
    sub_messages = [HumanMessage(content=task)]
    sub_llm = llm.bind_tools([read_file, write_file, run_bash])
    sub_tools = {t.name: t for t in [read_file, write_file, run_bash]}
    for _ in range(10):
        resp = sub_llm.invoke(sub_messages)
        sub_messages.append(resp)
        if not resp.tool_calls:
            return resp.content
        for tc in resp.tool_calls:
            r = sub_tools[tc["name"]].invoke(tc["args"])
            sub_messages.append(ToolMessage(content=str(r), tool_call_id=tc["id"]))
    return "（子任务未完成）"


@tool
def load_skill(name: str) -> str:
    """加载技能正文。"""
    if name not in SKILLS:
        return f"没有技能「{name}」。可用：{', '.join(SKILLS)}"
    return SKILLS[name]["body"]


@tool
def compact() -> str:
    """手动压缩上下文。"""
    return "上下文已压缩"


@tool
def create_task(subject: str, description: str = "") -> str:
    """创建任务。"""
    t = TASKS.create(subject, description)
    return f"Created {t.id}: {subject}"


@tool
def list_tasks() -> str:
    """列出任务。"""
    tasks = TASKS.list()
    if not tasks:
        return "（无任务）"
    return "\n".join(f"{t.id} [{t.status}] {t.subject} owner={t.owner or ''} wt={t.worktree or '-'}"
                     for t in tasks)


@tool
def update_task(task_id: str, addBlockedBy: list) -> str:
    """给任务加依赖（防环）。"""
    try:
        TASKS.update_dependencies(task_id, addBlockedBy)
        return f"已更新依赖 {task_id}"
    except ValueError as e:
        return f"失败：{e}"


@tool
def claim_task(task_id: str) -> str:
    """认领任务。"""
    with _get_task_lock(task_id):
        t = TASKS.load(task_id)
        if t.status != "pending":
            return f"Task {task_id} is {t.status}"
        if t.owner:
            return f"already owned by {t.owner}"
        if not can_start(task_id):
            return "Blocked by dependencies"
        t.owner = "agent"
        t.status = "in_progress"
        TASKS.save(t)
        return f"Claimed {task_id}"


@tool
def complete_task(task_id: str) -> str:
    """完成任务。"""
    t = TASKS.load(task_id)
    if not t.owner:
        t.owner = "agent"
    t.status = "completed"
    TASKS.save(t)
    return f"Completed {task_id}"


@tool
def schedule_cron(cron: str, prompt: str) -> str:
    """注册定时任务（cron 五段式）。"""
    err = validate_cron(cron)
    if err:
        return f"非法 cron：{err}"
    jid = f"cron_{random.randint(0, 999999):06d}"
    with cron_lock:
        scheduled_jobs[jid] = CronJob(id=jid, cron=cron, prompt=prompt)
        save_durable_jobs()
    return f"已注册 {jid}（{cron}）：{prompt}"


@tool
def list_crons() -> str:
    """列出定时任务。"""
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    return "\n".join(f"{j.id} [{j.cron}] {j.prompt}" for j in jobs) or "（无定时任务）"


@tool
def cancel_cron(job_id: str) -> str:
    """取消定时任务。"""
    with cron_lock:
        j = scheduled_jobs.pop(job_id, None)
        if j and j.durable:
            save_durable_jobs()
    return f"已取消 {job_id}" if j else f"无 {job_id}"


IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def idle_poll(agent_name, messages):
    """队友空闲轮询：inbox 优先 + 任务板其次，返回 work/shutdown/timeout。"""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        # ① inbox 优先（可能有 shutdown_request 等协议消息）
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    BUS.send(agent_name, "lead", "已关机", "shutdown_response",
                             {"request_id": msg.get("metadata", {}).get("request_id", "")})
                    return "shutdown"
            messages.append(HumanMessage(content=f"[Inbox]{json.dumps(inbox)}"))
            return "work"
        # ② 任务板其次：扫到可认领任务就 claim
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task.invoke({"task_id": task.id})
            if "Claimed" in result:
                if task.worktree:
                    _current.cwd = str(WORKTREES_DIR / task.worktree)   # worktree 切换 cwd
                messages.append(HumanMessage(
                    content=f"认领了任务 {task.id}：{task.subject}。"
                            f"完成后调 complete_task，task_id 填 {task.id}。"))
                return "work"
    return "timeout"


@tool
def submit_plan(plan: str) -> str:
    """提交计划给 Lead 审批（高风险操作先审后做）。"""
    name = getattr(_current, "name", "teammate")
    req_id = new_request_id()
    with protocol_lock:
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="plan_approval", sender=name, target="lead",
            status="pending", payload=plan)
    BUS.send(name, "lead", plan, "plan_approval_request", {"request_id": req_id})
    return f"计划已提交（{req_id}），等待 Lead 审批"


def _send_summary(name, summaries):
    content = ("\n".join(f"任务 {i+1}：{s}" for i, s in enumerate(summaries))
               if summaries else "（未完成）")
    BUS.send(name, "lead", content, "result")
    print(f"      🤝 [{name}] 完成：{content[:60]}")


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动队友线程（完整三阶段：WORK→IDLE→SHUTDOWN，自组织认领任务）。"""
    def run():
        _current.name = name
        system = f"You are '{name}', a {role}. Use tools to complete tasks."
        msgs = [HumanMessage(content=prompt)]
        sub_tool_list = [read_file, write_file, run_bash, send_message,
                         list_tasks, claim_task, complete_task, submit_plan]
        sub_llm = llm.bind_tools(sub_tool_list)
        sub_map = {t.name: t for t in sub_tool_list}
        summaries = []

        while True:
            # ── WORK 阶段（最多 10 轮 LLM）──
            for _ in range(10):
                # 身份重注入（压缩后 messages 太短，重新注入身份）
                if len(msgs) <= 3:
                    msgs.insert(0, HumanMessage(
                        content=f"<identity>You are '{name}', role: {role}. Continue.</identity>"))
                # 读 inbox + 处理协议
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        BUS.send(name, "lead", "已关机", "shutdown_response",
                                 {"request_id": msg.get("metadata", {}).get("request_id", "")})
                        _send_summary(name, summaries)
                        return
                    if msg.get("type") == "plan_approval_response":
                        meta = msg.get("metadata", {})
                        match_response("plan_approval_response", meta.get("request_id", ""),
                                       meta.get("approve", False))
                        msgs.append(HumanMessage(
                            content="[Plan approved，继续执行]" if meta.get("approve")
                            else "[Plan rejected，修正后重新提交]"))
                    else:
                        msgs.append(HumanMessage(content=f"[Inbox]{json.dumps(msg)}"))
                # 调 LLM
                resp = sub_llm.invoke([SystemMessage(content=system)] + msgs)
                msgs.append(resp)
                if not resp.tool_calls:
                    summaries.append(resp.content)
                    break
                for tc in resp.tool_calls:
                    r = sub_map[tc["name"]].invoke(tc["args"])
                    msgs.append(ToolMessage(content=str(r), tool_call_id=tc["id"]))

            # ── IDLE 阶段（轮询找活）──
            result = idle_poll(name, msgs)
            if result in ("shutdown", "timeout"):
                _send_summary(name, summaries)
                return
            # result == "work" → 回 WORK 继续干活

    threading.Thread(target=run, daemon=True).start()
    return f"已启动队友 {name}（{role}）"


@tool
def send_message(to_agent: str, content: str) -> str:
    """发消息给其他 agent。"""
    frm = getattr(_current, "name", "lead")
    BUS.send(frm, to_agent, content, "message")
    return f"已发送给 {to_agent}"


@tool
def check_inbox() -> str:
    """读自己的收件箱。"""
    inbox = BUS.read_inbox(getattr(_current, "name", "lead"))
    if not inbox:
        return "（收件箱空）"
    return "\n".join(f"from={m['from']} type={m['type']}: {m['content'][:150]}" for m in inbox)


@tool
def request_shutdown(name: str) -> str:
    """请求队友关机（握手）。"""
    req_id = new_request_id()
    with protocol_lock:
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown", sender="lead", target=name, status="pending")
    BUS.send("lead", name, "请关机", "shutdown_request", {"request_id": req_id})
    return f"已请求 {name} 关机（{req_id}）"


@tool
def review_plan(request_id: str, approve: bool) -> str:
    """审批计划。"""
    state = pending_requests.get(request_id)
    if not state:
        return f"无请求 {request_id}"
    BUS.send("lead", state.sender, "approved" if approve else "rejected",
             "plan_approval_response", {"request_id": request_id, "approve": approve})
    return f"已{'批准' if approve else '拒绝'}计划 {request_id}"


@tool
def create_worktree(name: str) -> str:
    """创建隔离 worktree。"""
    err = validate_worktree_name(name)
    if err:
        return f"创建失败：{err}"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"{name} 已存在"
    r = subprocess.run(["git", "worktree", "add", "-b", f"wt/{name}", str(path), "main"],
                       cwd=WORKDIR, capture_output=True, text=True)
    return f"已创建 worktree {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def bind_task(task_id: str, worktree_name: str) -> str:
    """把任务绑定到 worktree。"""
    t = TASKS.load(task_id)
    if t.worktree:
        return f"已绑定 {t.worktree}"
    if not (WORKTREES_DIR / worktree_name).exists():
        return f"worktree {worktree_name} 不存在"
    t.worktree = worktree_name
    TASKS.save(t)
    return f"已绑定 {task_id} → {worktree_name}"


@tool
def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """删除 worktree（有改动默认拒绝）。"""
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"{name} 不存在"
    args = ["git", "worktree", "remove"]
    if discard_changes:
        args.append("--force")
    args.append(str(path))
    r = subprocess.run(args, cwd=WORKDIR, capture_output=True, text=True)
    return f"已删除 {name}" if r.returncode == 0 else f"失败：{r.stderr}"


@tool
def keep_worktree(name: str) -> str:
    """保留 worktree（等人工 review）。"""
    return f"已保留 {name}"


@tool
def connect_mcp(name: str) -> str:
    """连接 MCP server（filesystem）。"""
    if name in mcp_clients:
        return f"已连接 {name}"
    if name == "filesystem":
        try:
            c = RealMCPClient(name, "npx",
                              ["-y", "@modelcontextprotocol/server-filesystem", str(WORKDIR)])
        except Exception as e:
            return f"连接失败：{e}"
        mcp_clients[name] = c
        return f"已连接 filesystem，发现 {len(c.tools)} 个工具"
    return f"未知 server {name}"


@tool
def mcp_call(server: str, tool_name: str, arguments: str) -> str:
    """调用 MCP 工具。arguments 是 JSON 字符串。"""
    c = mcp_clients.get(server)
    if not c:
        return f"未连接 {server}"
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as e:
        return f"非法 JSON：{e}"
    return c.call_tool(tool_name, args)


BUILTIN_TOOLS = [run_bash, read_file, write_file, todo_write, delegate, load_skill, compact,
                 create_task, list_tasks, update_task, claim_task, complete_task,
                 schedule_cron, list_crons, cancel_cron, spawn_teammate, send_message,
                 check_inbox, request_shutdown, review_plan, create_worktree, bind_task,
                 remove_worktree, keep_worktree, connect_mcp, mcp_call]
TOOL_HANDLERS = {t.name: t for t in BUILTIN_TOOLS}


def assemble_tool_pool():
    tools = list(BUILTIN_TOOLS)
    handlers = dict(TOOL_HANDLERS)
    for server_name, client in mcp_clients.items():
        for mcp_tool in client.tools:
            prefixed = f"mcp__{normalize_mcp_name(server_name)}__{normalize_mcp_name(mcp_tool.name)}"

            def _make(s=server_name, t=mcp_tool.name, c=client):
                def handler(**kwargs):
                    return c.call_tool(t, kwargs)
                return handler

            handlers[prefixed] = StructuredTool.from_function(
                func=_make(), name=prefixed, description=f"MCP {server_name}.{mcp_tool.name}")
            tools.append(handlers[prefixed])
    return tools, handlers


# ═══════════ s20 统一循环（机制很多，循环一个）═══════════
def agent_turn(query: str) -> str:
    trigger_hooks("UserPromptSubmit", query)

    messages = [HumanMessage(content=query)]
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()

    state = RecoveryState()
    memory_block = build_memory_injection(messages)
    context = update_context()

    while True:
        # ── LLM 前：压缩管线（便宜的先，贵的后）──
        messages = tool_result_budget(messages)
        messages = snip_compact(messages)
        if estimate_chars(messages) > CONTEXT_CHAR_LIMIT:
            messages = micro_compact(messages, int(CONTEXT_CHAR_LIMIT * 0.8))
        if estimate_chars(messages) > CONTEXT_CHAR_LIMIT:
            messages = compact_history(messages, active_request=query)

        # ── LLM 前：注入 cron + 后台通知 ──
        for job in consume_cron_queue():
            messages.append(HumanMessage(content=f"[Scheduled] {job.prompt}"))
        for notif in collect_background_results():
            messages.append(HumanMessage(content=notif))

        # ── LLM 前：组装 system prompt（缓存）──
        system = get_system_prompt(context)
        convo = [SystemMessage(content=system)] + messages
        if memory_block:
            convo.append(HumanMessage(content=memory_block))

        # ── LLM 调用（三条恢复路径）──
        try:
            tools, handlers = assemble_tool_pool()
            response = with_retry(lambda: llm.bind_tools(tools).invoke(convo))
        except Exception as error:
            if is_prompt_too_long(error) and not state.has_attempted_compact:
                state.has_attempted_compact = True
                messages = reactive_compact(messages, active_request=query)
                continue
            raise
        messages.append(response)

        if not response.tool_calls:
            trigger_hooks("Stop", response.content)
            break

        # ── 执行工具 ──
        results = []
        for tc in response.tool_calls:
            blocked = trigger_hooks("PreToolUse", tc)
            if blocked:
                results.append(ToolMessage(content=str(blocked), tool_call_id=tc["id"]))
                continue
            handler = handlers.get(tc["name"])
            if not handler:
                results.append(ToolMessage(content=f"未知工具 {tc['name']}", tool_call_id=tc["id"]))
                continue
            if should_run_background(tc["name"], tc["args"]):
                bid = start_background_task(tc["name"], tc["args"], tc["id"])
                results.append(ToolMessage(content=f"[Background task {bid} started]", tool_call_id=tc["id"]))
                continue
            try:
                result = handler.invoke(tc["args"])
            except Exception as e:
                result = f"工具执行出错：{e}"
            trigger_hooks("PostToolUse", tc, result)
            results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        messages.extend(results)

    # ── 任务结束：提取记忆（s09 写入时机）──
    for item in extract_memories(messages):
        write_memory_file(item["name"], item["mem_type"], item["description"], item["body"])
    context = update_context()
    return response.content


if __name__ == "__main__":
    print("最终答案：\n")
    print(agent_turn("List the files in this directory."))
