"""s14: Cron Scheduler —— 按时间表生产工作，调度与执行解耦

课程：learn-claudecode s14
主旨：「给 Agent 装个闹钟」—— 独立调度线程判断时间，队列传递触发。

四层模型：
  Scheduler（调度线程，每秒轮询）→ Queue（cron_queue）→ Queue Processor（空闲交付）→ Consumer（agent_loop 注入）

cron 五段式：分钟 小时 日 月 星期（* / */N / N / N-M / N,M,...）
  DOM（日）和 DOW（星期）同时被约束时，任一匹配即可（OR）—— Vixie cron 的历史语义。
"""
import json
import os
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

WORKDIR = Path.cwd()
SCHEDULED_FILE = WORKDIR / ".scheduled_tasks.json"


# ═══════════ 数据结构 ═══════════
@dataclass
class CronJob:
    id: str
    cron: str        # "0 9 * * *" 五段式
    prompt: str      # 触发时注入给 Agent 的消息
    recurring: bool  # True=周期，False=一次性
    durable: bool    # True=写磁盘，跨会话保留


# ═══════════ 状态（内存 + 锁）═══════════
scheduled_jobs: dict = {}      # job_id → CronJob
cron_queue: list = []          # 已触发的 job（调度线程写，agent_loop 消费）
cron_lock = threading.Lock()   # 保护 scheduled_jobs / cron_queue
agent_lock = threading.Lock()  # 标记 Agent 是否空闲（queue processor 用）
_last_fired: dict = {}         # job_id → "YYYY-MM-DD HH:MM"（防同一分钟重复触发）
_bg_counter = 0


# ═══════════ 模块 1：cron 匹配 ═══════════
def _cron_field_matches(field: str, value: int) -> bool:
    """单个字段是否匹配。支持 *  */N  N  N-M  N,M,..."""
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if "/" in part:                       # */N 或 N-M/N
            base, _, step = part.partition("/")
            step = int(step)
            if base == "*":
                if value % step == 0:
                    return True
            elif "-" in base:                 # N-M/N
                lo, hi = map(int, base.split("-"))
                if lo <= value <= hi and (value - lo) % step == 0:
                    return True
        elif "-" in part:                     # N-M
            lo, hi = map(int, part.split("-"))
            if lo <= value <= hi:
                return True
        else:                                 # N
            if int(part) == value:
                return True
    return False


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """datetime 是否命中 cron 五段式表达式。

    ⭐ 组合逻辑：分钟/小时/月 必须全匹配（AND）；
       DOM（日）和 DOW（星期）同时被约束时，任一匹配即可（OR）—— Vixie cron 语义。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7   # Python 周一=0 → cron 周日=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    month_ok = _cron_field_matches(month, dt.month)
    dom_ok = _cron_field_matches(dom, dt.day)
    dow_ok = _cron_field_matches(dow, dow_val)

    if not (m and h and month_ok):
        return False
    dom_constrained = dom != "*"
    dow_constrained = dow != "*"
    if dom_constrained and dow_constrained:
        return dom_ok or dow_ok       # 两个都约束 → OR
    if dom_constrained:
        return dom_ok
    if dow_constrained:
        return dow_ok
    return True                       # 都不约束 → 无条件通过


# ═══════════ 模块 2：校验 + 注册 ═══════════
CRON_RANGES = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31),
               "month": (1, 12), "dow": (0, 6)}


def validate_cron(cron: str) -> str:
    """校验 cron 表达式，非法返回错误信息，合法返回空字符串"""
    fields = cron.strip().split()
    if len(fields) != 5:
        return f"cron 需要 5 段（分钟 小时 日 月 星期），实际 {len(fields)} 段"
    names = ["minute", "hour", "dom", "month", "dow"]
    for name, field in zip(names, fields):
        lo, hi = CRON_RANGES[name]
        for part in field.split(","):
            base = part.split("/")[0]
            if base == "*":
                continue
            try:
                if "-" in base:
                    a, b = map(int, base.split("-"))
                    if a < lo or b > hi or a > b:
                        return f"字段 {name}={part} 超出范围 [{lo}, {hi}]"
                else:
                    v = int(base)
                    if v < lo or v > hi:
                        return f"字段 {name}={part} 超出范围 [{lo}, {hi}]"
            except ValueError:
                return f"字段 {name}={part} 不是合法数字"
    return ""


def _new_id() -> str:
    global _bg_counter
    _bg_counter += 1
    return f"cron_{_bg_counter:04d}"


def save_durable_jobs() -> None:
    """把 durable 任务写进 .scheduled_tasks.json"""
    tasks = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    SCHEDULED_FILE.write_text(json.dumps({"tasks": tasks}, indent=2),
                              encoding="utf-8")


def load_durable_jobs() -> None:
    """启动时从 .scheduled_tasks.json 恢复持久化任务（坏任务跳过，不拖垮启动）"""
    if not SCHEDULED_FILE.exists():
        return
    try:
        data = json.loads(SCHEDULED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for item in data.get("tasks", []):
        if not item.get("id") or not item.get("cron") or not item.get("prompt"):
            continue
        if validate_cron(item["cron"]):            # 坏表达式跳过
            continue
        job = CronJob(id=item["id"], cron=item["cron"], prompt=item["prompt"],
                      recurring=bool(item.get("recurring", True)),
                      durable=True)
        scheduled_jobs[job.id] = job


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> str:
    """注册一个定时任务。校验失败返回错误信息，成功返回任务 ID。"""
    err = validate_cron(cron)
    if err:
        return f"非法 cron 表达式：{err}"
    job = CronJob(id=_new_id(), cron=cron, prompt=prompt,
                  recurring=recurring, durable=durable)
    with cron_lock:
        scheduled_jobs[job.id] = job
        if durable:
            save_durable_jobs()
    kind = "周期" if recurring else "一次性"
    persist = "持久化" if durable else "会话级"
    return f"已注册 {kind}任务 {job.id}（{cron}，{persist}）：{prompt}"


def cancel_job(job_id: str) -> str:
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        if job is None:
            return f"没有任务 {job_id}"
        if job.durable:
            save_durable_jobs()
    return f"已取消任务 {job_id}（{job.cron}）"


def list_jobs() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "（没有定时任务）"
    return "\n".join(f"{j.id}  [{j.cron}]  {'周期' if j.recurring else '一次性'}"
                     f"  {j.prompt}" for j in jobs)


# ═══════════ 模块 3：调度线程 ═══════════
def cron_scheduler_loop() -> None:
    """daemon 调度线程：每秒轮询，时间到了塞进 cron_queue。

    关键设计：
    - 独立于 agent_loop（即使 Agent 没在跑，也在检查时间）
    - minute_marker 防同一分钟重复触发（date-aware，不会跨天跳过）
    - 单 job try/except（一个坏 job 不拖垮整个线程）
    """
    while True:
        time.sleep(1)
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"      ⏰ [cron] {job.id} 触发（{now:%H:%M:%S}）")
                        if not job.recurring:        # 一次性：触发后删除
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"      ⚠️ [cron] 任务 {job.id} 出错：{e}")


# ═══════════ 模块 4：queue processor + 消费 ═══════════
def has_cron_queue() -> bool:
    with cron_lock:
        return len(cron_queue) > 0


def consume_cron_queue() -> list:
    """取出队列里所有已触发的 job（消费后清空）"""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def queue_processor_loop(get_prompt_fn) -> None:
    """daemon 队列处理器：队列非空 + Agent 空闲 → 自动拉起一轮执行"""
    while True:
        time.sleep(0.2)
        if not has_cron_queue():
            continue
        if not agent_lock.acquire(blocking=False):   # Agent 忙，跳过
            continue
        try:
            if has_cron_queue():
                get_prompt_fn()                       # 拉一轮 agent_loop
        finally:
            agent_lock.release()


# ═══════════ 工具 ═══════════
@tool
def schedule_cron(cron: str, prompt: str, recurring: bool = True,
                  durable: bool = True) -> str:
    """注册定时任务。cron 是五段式（分 时 日 月 星期），如 "0 9 * * *"=每天9点、"*/5 * * * *"=每5分钟。"""
    return schedule_job(cron, prompt, recurring, durable)


@tool
def list_crons() -> str:
    """列出所有定时任务。"""
    return list_jobs()


@tool
def cancel_cron(job_id: str) -> str:
    """取消指定定时任务。"""
    return cancel_job(job_id)


@tool
def run_bash(command: str) -> str:
    """执行 shell 命令。"""
    import subprocess
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return (r.stdout or r.stderr or "(无输出)").strip()


TOOLS = [schedule_cron, list_crons, cancel_cron, run_bash]
TOOL_MAP = {t.name: t for t in TOOLS}
llm_with_tools = llm.bind_tools(TOOLS)

SYSTEM = "你是任务执行助手。用定时任务工具（schedule_cron/list_crons/cancel_cron）管理周期任务。"


# ═══════════ agent_loop（消费 cron 队列）═══════════
def agent_loop(query: str | None = None):
    """执行一轮：先消费 cron 队列，再处理用户输入"""
    messages = []
    if query:
        messages.append(HumanMessage(content=query))

    # 消费 cron 队列：已触发的任务注入为 "[Scheduled] prompt"
    fired = consume_cron_queue()
    for job in fired:
        messages.append(HumanMessage(content=f"[Scheduled] {job.prompt}"))

    if not messages:
        return

    while True:
        response = llm_with_tools.invoke([SystemMessage(content=SYSTEM)] + messages)
        messages.append(response)
        if not response.tool_calls:
            print(f"\n      💬 {response.content[:200]}")
            break
        for tc in response.tool_calls:
            result = TOOL_MAP[tc["name"]].invoke(tc["args"])
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


def start_scheduler() -> None:
    """启动调度线程 + 队列处理器（都 daemon，进程退出跟着停）"""
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    threading.Thread(target=queue_processor_loop, args=(agent_loop,),
                     daemon=True).start()
    print("🕐 调度线程已启动（每秒轮询），队列处理器等待交付")


if __name__ == "__main__":
    start_scheduler()

    # 演示：注册一个每 2 分钟的任务（durable），启动后调度线程会自动触发
    print("\n" + schedule_job("*/2 * * * *", "run date to show current time",
                              recurring=True, durable=True))
    print(schedule_job("0 9 * * 1-5", "run the daily test suite",
                       recurring=True, durable=True))
    print("\n当前任务：")
    print(list_jobs())
    print("\n等待调度线程触发...（Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出。durable 任务已写入 .scheduled_tasks.json，下次启动自动恢复。")
