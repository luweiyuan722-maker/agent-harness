import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage

load_dotenv()

llm = ChatOpenAI(model="deepseek-chat",
                 base_url="https://api.deepseek.com/v1",
                 api_key=os.environ.get("DEEPSEEK_API_KEY"))

@tool
def get_current_time():
    """Get the current time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

TOOLS = {"get_current_time": get_current_time}

llm_with_tools = llm.bind_tools([get_current_time])

query = "现在几点了？"
messages = [HumanMessage(content=query)]

round_no = 0
while True:
    round_no += 1
    print(f"[第 {round_no} 轮] 调模型…")
    response = llm_with_tools.invoke(messages)
    messages.append(response)
    if not response.tool_calls:
        print(f"[第 {round_no} 轮] 模型不再调工具 → 结束")
        print("最终答案:", response.content)
        break
    for tc in response.tool_calls:
        print(f"[第 {round_no} 轮] 要调工具: {tc['name']}({tc['args']})")
        result = TOOLS[tc["name"]].invoke(tc["args"])
        print(f"[第 {round_no} 轮] 工具返回: {result}")
        messages.append(ToolMessage(
            content=str(result), tool_call_id=tc["id"]))
