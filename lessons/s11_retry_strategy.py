"""s11: Error Recovery —— 错误不是终点，是重试的起点

课程：learn-claudecode s11
主旨：「分类失败，决定哪种重试值得」—— 三种故障模式，三条恢复路径。

  路径 1  输出截断（finish_reason == length）→ 升级 max_tokens 8K→64K，再截断就续写提示（最多 3 次）
  路径 2  上下文超限（prompt_too_long）      → reactive compact 压缩一次 → 重试
  路径 3  临时故障（429/529）                → 指数退避 + 抖动，连续 529 切换备用模型

设计哲学：确定性故障（截断/超限）改条件重试 1 次就够；随机性故障（限流/过载）指数退避多次重试。
判断标准 = 「重试能不能改变结果」。
"""
import os
import random
import time
from dataclasses import dataclass
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage

load_dotenv()

# ── LLM ──
llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

# ── s11 新增常量 ──
MAX_TOKENS = 8000                # 默认输出上限
ESCALATED_MAX_TOKENS = 64000     # 升级后的输出上限（8 倍）
MAX_RECOVERY_RETRIES = 3         # 续写提示最多 3 次
MAX_RETRIES = 10                 # 临时故障退避最多 10 次
BASE_DELAY_MS = 500              # 退避基数（毫秒）
MAX_DELAY_MS = 32000             # 退避封顶（毫秒）
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL_ID")   # 可选备用模型，没配就是 None
OVERLOAD_SWITCH_THRESHOLD = 3    # 连续几次 529 才切换备用模型

CONTINUATION_PROMPT = ("Output token limit hit. Resume directly — "
                       "no apology, no recap. Pick up mid-thought.")

# ── 上下文超限的关键词（复用 s08 思路）──
TOO_LONG_MARKERS = (
    "prompt_too_long",
    "context_length_exceeded",
    "maximum context length",
    "too many tokens",
    "exceeds the maximum",
    "reduce the length",
)

# ── 临时故障的关键词 ──
RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "529",
    "overloaded",
)


# ═══════════ 模块 1：RecoveryState + 错误分类 ═══════════
@dataclass
class RecoveryState:
    """记录这次会话已经尝试过哪些恢复动作，防止无限重试"""
    has_escalated: bool = False          # 输出截断：是否已把 max_tokens 升级过
    recovery_count: int = 0              # 输出截断：已续写几次
    has_attempted_compact: bool = False  # 上下文超限：是否已压缩过
    consecutive_overloads: int = 0       # 临时故障：连续 529 次数（用于切模型）


def is_truncated(response) -> bool:
    """路径 1 的判断：模型输出是否被 max_tokens 截断"""
    reason = response.response_metadata.get("finish_reason")
    return reason == "length"


def is_prompt_too_long(error) -> bool:
    """路径 2 的判断：这个异常是不是「上下文超限」"""
    text = str(error).lower()
    return any(marker in text for marker in TOO_LONG_MARKERS)


def is_rate_limited(error) -> bool:
    """路径 3 的判断：这个异常是不是「限流/过载」（临时故障，值得重试）"""
    text = str(error).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


# ═══════════ 模块 2：指数退避 + 抖动 ═══════════
def retry_delay(attempt: int, retry_after=None) -> float:
    """计算第 attempt 次重试该等多久（秒）。

    指数退避：500ms → 1s → 2s → 4s → 8s → 16s → 32s（封顶）
    抖动：在基础值上再随机加 0~25%，让并发请求不在同一时刻重试（防惊群）。
    """
    if retry_after:                      # 服务端明确说了等多久，就听它的
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), MAX_DELAY_MS) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, max_retries: int = MAX_RETRIES):
    """对任意函数做指数退避重试：限流/过载时等待重试，其他错误直接抛。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as error:
            if not is_rate_limited(error):   # 非临时故障 → 不重试，直接抛
                raise
            delay = retry_delay(attempt)
            print(f"      🔁 [with_retry] 限流/过载，{delay:.2f}s 后重试 "
                  f"（{attempt + 1}/{max_retries}）")
            time.sleep(delay)
    raise RuntimeError(f"重试 {max_retries} 次仍失败")


# ═══════════ 路径 2 的恢复动作：reactive compact（简化版）═══════════
def reactive_compact(messages: list, keep_newest: int = 5) -> list:
    """上下文超限后的抢救式压缩：只保住最新 keep_newest 条，前面用占位消息替代。

    ⚠️ 和 s08 的边界坑一样：tail 的第一条若是工具结果，它的「调用」必须一起带过来，
       否则 tool_call/tool_result 配对断裂 → API 又报错 → 白压。
    """
    if len(messages) <= keep_newest:
        return list(messages)

    split = len(messages) - keep_newest
    while (split > 0 and isinstance(messages[split], ToolMessage)
           and getattr(messages[split - 1], "tool_calls", None)):
        split -= 1

    header = HumanMessage(
        content=f"[Reactive compact — 上下文超限，压缩了前 {split} 条历史]")
    return [header] + list(messages[split:])


# ═══════════ 工具（s11 只留一个，聚焦错误恢复）═══════════
@tool
def run_bash(command: str) -> str:
    """执行一条 shell 命令并返回输出。"""
    import subprocess
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


tools = [run_bash]
llm_with_tools = llm.bind_tools(tools)
TOOLS = {t.name: t for t in tools}

SYSTEM = "You are a coding agent. Act, don't explain. Use tools to solve tasks."


# ═══════════ 模块 3：三条恢复路径集成进 agent_loop ═══════════
def agent_loop(query: str):
    state = RecoveryState()
    messages: list = [HumanMessage(content=query)]
    max_tokens = MAX_TOKENS
    round_no = 0

    while True:
        round_no += 1
        print(f"──── 第 {round_no} 轮 ────  max_tokens={max_tokens}")

        # ── 用 with_retry 包住 LLM 调用：限流/过载自动退避重试 ──
        try:
            response = with_retry(
                lambda: llm_with_tools.invoke(
                    [SystemMessage(content=SYSTEM)] + messages,
                    max_tokens=max_tokens,
                )
            )
        except Exception as error:
            # 路径 2：上下文超限 → 压缩一次 → 重试
            if is_prompt_too_long(error) and not state.has_attempted_compact:
                messages = reactive_compact(messages)
                state.has_attempted_compact = True
                print("      🚑 [reactive compact] 上下文超限，压缩后重试")
                continue
            # 其他错误（含压缩过还超限）→ 抛出去
            raise

        # 路径 1：输出截断
        if is_truncated(response):
            if not state.has_escalated:
                # 第一次：升级 max_tokens，不追加截断输出，重试同一请求
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print("      📈 [escalate] 输出截断，max_tokens 8K→64K，重试")
                continue
            # 64K 还是截断：保存截断输出 + 续写提示（最多 3 次）
            messages.append(response)
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append(HumanMessage(content=CONTINUATION_PROMPT))
                state.recovery_count += 1
                print(f"      ✍️ [continue] 64K 仍截断，续写提示"
                      f"（{state.recovery_count}/{MAX_RECOVERY_RETRIES}）")
                continue
            print("      ⛔ 续写 3 次仍截断，退出")
            return

        # 正常：追加响应
        messages.append(response)

        if not response.tool_calls:
            print("\n══════ 最终答案 ══════")
            print(response.content)
            return

        for tc in response.tool_calls:
            print(f"    → 调用 {tc['name']}({str(tc['args'])[:60]})")
            result = TOOLS[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


if __name__ == "__main__":
    agent_loop("列出当前目录下的文件")
