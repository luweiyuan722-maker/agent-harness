"""s10: Task System —— 从执行清单到可协调的任务状态

课程：learn-claudecode s10
主旨：「大目标拆成小任务，排好序，持久化」—— 文件持久化的任务图（DAG），多 Agent 协作的基础。

相比 s05 TodoWrite 的进化：
  每个任务有独立 ID + 状态 + blockedBy（依赖）+ owner（分工）
  持久化到 .tasks/{id}.json，跨会话可恢复

状态机：pending ──claim──→ in_progress ──complete──→ completed
两阶段构建：先 create 所有节点（拿运行时 ID），再 update 加依赖边
"""
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, asdict
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
TASKS_DIR = WORKDIR / ".tasks"
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")


# ═══════════ Task 数据结构 + TaskStore ═══════════
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str              # pending | in_progress | completed
    owner: str | None        # 谁认领了它
    blockedBy: list          # 依赖的任务 ID 列表


class TaskStore:
    """校验 ID、读写 .tasks/{id}.json 的任务存储。

    两个安全设计（网站源码里比 README 更细的细节）：
    1. _path 用 TASK_ID_PATTERN 校验 ID 格式，防路径注入（task_id="../../etc/passwd"）
    2. _root/_path 用 resolve() + is_relative_to 防路径逃逸出工作目录
    """

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
            task = Task(id=f"task_{secrets.token_hex(4)}",
                        subject=subject, description=description,
                        status="pending", owner=None, blockedBy=[])
            try:
                # "x" 模式 = 排他创建：文件已存在则 FileExistsError，重新生成 ID
                with self._path(task.id, create_root=True).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """task_id 是否（传递地）依赖 target_id —— 用于防环（BFS）"""
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
            raise ValueError("addBlockedBy must be a list of task IDs")
        task = self.load(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError("dependencies can only be updated while pending and unowned")
        dependencies = list(dict.fromkeys(add_blocked_by))   # 去重，保序
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                raise ValueError(f"Dependency cycle detected: {task_id} -> {dependency}")
        # 先整体校验，再统一提交（避免"加了一半才发现错"）
        task.blockedBy.extend(d for d in dependencies if d not in task.blockedBy)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), indent=2), encoding="utf-8")

    def load(self, task_id: str) -> Task:
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status: {task.status}")
        return task

    def list(self) -> list:
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(path.stem) for path in sorted(root.glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)


# ═══════════ 任务核心逻辑 ═══════════
def incomplete_dependencies(task: Task) -> list:
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if TASKS.load(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            incomplete.append(dependency)   # 依赖文件不存在 = 视为未完成
    return incomplete


def can_start(task_id: str) -> bool:
    return not incomplete_dependencies(TASKS.load(task_id))


def _claim_task(task_id: str, owner: str = "agent") -> str:
    task = TASKS.load(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    deps = incomplete_dependencies(task)
    if deps:
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    TASKS.save(task)
    return f"Claimed {task.id} ({task.subject})"


def _complete_task(task_id: str, owner: str = "agent") -> str:
    task = TASKS.load(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.owner != owner:
        return f"Task {task_id} is owned by {task.owner}, not {owner}"
    # 快照：完成前已经 ready 的下游（用于找出"刚被解锁"的）
    ready_before = {t.id for t in TASKS.list()
                    if t.status == "pending" and t.blockedBy and can_start(t.id)}
    task.status = "completed"
    TASKS.save(task)
    unblocked = [t.subject for t in TASKS.list()
                 if t.status == "pending" and t.blockedBy
                 and t.id not in ready_before and can_start(t.id)]
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


# ═══════════ 工具（LangChain @tool）══════════
@tool
def create_task(subject: str, description: str = "") -> str:
    """创建一个新任务节点，返回运行时生成的 task ID。先创建所有节点，再用 update_task 加依赖。"""
    task = TASKS.create(subject, description)
    return f"Created {task.id} ({task.subject})"


@tool
def update_task(task_id: str, addBlockedBy: list) -> str:
    """给任务添加依赖（blockedBy）。用 create_task 返回的 ID。"""
    task = TASKS.update_dependencies(task_id, addBlockedBy)
    return f"Updated {task.id}, blockedBy now: {task.blockedBy}"


@tool
def claim_task(task_id: str) -> str:
    """认领任务：pending → in_progress。依赖未全部完成会被拒绝。"""
    return _claim_task(task_id)


@tool
def complete_task(task_id: str) -> str:
    """完成任务：in_progress → completed。会解锁下游任务。"""
    return _complete_task(task_id)


@tool
def get_task(task_id: str) -> str:
    """查看单个任务的完整 JSON（含描述和依赖）。"""
    return json.dumps(asdict(TASKS.load(task_id)), indent=2)


@tool
def list_tasks() -> str:
    """列出所有任务及其状态和依赖。"""
    tasks = TASKS.list()
    if not tasks:
        return "（没有任务）"
    lines = []
    for t in tasks:
        line = f"{t.id}  [{t.status}]  {t.subject}"
        if t.blockedBy:
            line += f"  <- {t.blockedBy}"
        lines.append(line)
    return "\n".join(lines)


@tool
def run_bash(command: str) -> str:
    """执行一个 shell 命令，返回输出。"""
    result = subprocess.run(command, shell=True, capture_output=True,
                            text=True, timeout=60)
    return result.stdout + (result.stderr if result.stderr else "")


TOOLS = [create_task, update_task, claim_task, complete_task,
         get_task, list_tasks, run_bash]
TOOL_MAP = {t.name: t for t in TOOLS}
llm_with_tools = llm.bind_tools(TOOLS)

SYSTEM = """你是任务执行助手，用任务工具追踪依赖和进度。

规则：
1. 先 create_task 创建所有任务节点，拿到运行时生成的 ID
2. 再用 update_task 用那些 ID 添加依赖（blockedBy）
3. claim_task 认领第一个可做的任务（依赖已完成的），complete_task 完成它
4. 认领被拒绝（Blocked）时，说明有依赖没完成，先做依赖任务
"""


def agent_loop(query: str):
    messages = [HumanMessage(content=query)]
    round_no = 0
    while True:
        round_no += 1
        response = llm_with_tools.invoke([SystemMessage(content=SYSTEM)] + messages)
        messages.append(response)
        if not response.tool_calls:
            print("\n══════ 最终答案 ══════")
            print(response.content)
            break
        for tc in response.tool_calls:
            fn = TOOL_MAP[tc["name"]]
            try:
                result = fn.invoke(tc["args"])
            except Exception as error:
                result = f"Error: {error}"
            print(f"  第{round_no}轮 🔧 {tc['name']}{tc['args']}")
            print(f"       → {str(result)[:200]}")
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))


if __name__ == "__main__":
    import shutil
    shutil.rmtree(".tasks", ignore_errors=True)   # 清空，干净起跑
    query = ("Create tasks: setup database schema, "
             "create API endpoints (depends on schema), "
             "write tests (depends on endpoints), "
             "write docs (depends on schema). "
             "Then claim and complete them in the correct order.")
    agent_loop(query)
    print("\n══════ 最终 .tasks/ 目录 ══════")
    for f in sorted(TASKS_DIR.glob("*.json")):
        print(f"  {f.name}")
