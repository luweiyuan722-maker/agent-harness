import importlib.util
from langchain_core.messages import HumanMessage, ToolMessage

spec = importlib.util.spec_from_file_location(
    "s11", "/Users/shikanoko/Desktop/agent_harness/lessons/s11_retry_strategy.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

mod.time.sleep = lambda s: None   # 测试时不真等


class FakeResponse:
    def __init__(self, reason):
        self.response_metadata = {"finish_reason": reason}


class FakeError(Exception):
    pass


# ── 1. 三个判断函数 ──
assert mod.is_truncated(FakeResponse("length")) is True
assert mod.is_truncated(FakeResponse("stop")) is False
assert mod.is_truncated(FakeResponse("tool_calls")) is False
print("✅ is_truncated：length=True / stop=False / tool_calls=False")

assert mod.is_prompt_too_long(FakeError("Error: prompt_too_long")) is True
assert mod.is_prompt_too_long(FakeError("Error: 401 unauthorized")) is False
print("✅ is_prompt_too_long：prompt_too_long=True / 401=False")

assert mod.is_rate_limited(FakeError("429 rate limit exceeded")) is True
assert mod.is_rate_limited(FakeError("529 overloaded")) is True
assert mod.is_rate_limited(FakeError("500 internal error")) is False
print("✅ is_rate_limited：429=True / 529=True / 500=False")

# ── 2. retry_delay：指数增长 + 封顶 + retry_after 优先 ──
d0 = mod.retry_delay(0)
d3 = mod.retry_delay(3)
d10 = mod.retry_delay(10)
assert 0.5 <= d0 <= 0.625, d0          # 500ms + 0~25% 抖动
assert 4.0 <= d3 <= 5.0, d3            # 4000ms + 抖动
assert 32.0 <= d10 <= 40.0, d10        # 封顶 32000ms + 抖动
assert mod.retry_delay(0, retry_after=0.1) == 0.1   # 服务端说了等多久就听它的
print(f"✅ retry_delay：0.5s→4s→封顶32s（实测 {d0:.2f}/{d3:.2f}/{d10:.2f}），retry_after 优先")

# ── 3. with_retry：限流重试成功 / 非限流直接抛 ──
calls = {"n": 0}
def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise FakeError("429 rate limit")
    return "success"

assert mod.with_retry(flaky) == "success"
assert calls["n"] == 3
print(f"✅ with_retry：限流 2 次后第 3 次成功（共 {calls['n']} 次调用）")

def always_401():
    raise FakeError("401 unauthorized")

try:
    mod.with_retry(always_401)
    assert False, "非限流错误应该直接抛"
except FakeError:
    pass
print("✅ with_retry：非限流错误（401）直接抛，不重试")

# ── 4. reactive_compact：保最新 5 条 + header ──
msgs = [HumanMessage(content=f"m{i}") for i in range(10)]
out = mod.reactive_compact(msgs, keep_newest=5)
assert len(out) == 6, len(out)              # 1 header + 5 条
assert "Reactive compact" in out[0].content
assert [m.content for m in out[1:]] == ["m5", "m6", "m7", "m8", "m9"]
print(f"✅ reactive_compact：10 条 → 1 header + 最新 5 条（{len(out)} 条）")

print("\n════════ 全部通过 ════════")
