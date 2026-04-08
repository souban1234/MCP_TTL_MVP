import sys
import os
import json
import traceback
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import Field, create_model
from typing import Any
from urllib.parse import urlparse as _urlparse

load_dotenv()

# -------------------------
# PROJECT ROOT
# -------------------------
if getattr(sys, 'frozen', False):
    project_root = os.path.dirname(sys.executable)
else:
    current_script_path = os.path.abspath(__file__)
    client_dir = os.path.dirname(current_script_path)
    project_root = os.path.abspath(os.path.join(client_dir, ".."))


# -------------------------
# JSON NORMALIZATION (LIKE YOUR REF CLIENT)
# -------------------------
def _make_json_safe(value):
    if hasattr(value, "model_dump"):
        return _make_json_safe(value.model_dump())

    from dataclasses import asdict, is_dataclass
    if is_dataclass(value):
        return _make_json_safe(asdict(value))

    if isinstance(value, dict):
        return {str(k): _make_json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_make_json_safe(v) for v in value]

    return value


def _parse_json_text(value):
    if not isinstance(value, str):
        return value

    try:
        return _make_json_safe(json.loads(value))
    except:
        return value


def _normalize_tool_result(result):
    data = getattr(result, "data", None)
    if data is not None:
        if isinstance(data, str):
            return _parse_json_text(data)
        return _make_json_safe(data)

    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return _make_json_safe(structured["result"])
        return _make_json_safe(structured)

    if isinstance(result, dict):
        if set(result.keys()) == {"result"}:
            return _make_json_safe(result["result"])
        return _make_json_safe(result)

    if isinstance(result, str):
        return _parse_json_text(result)

    content = getattr(result, "content", None) or []
    texts = [c.text for c in content if hasattr(c, "text")]

    if texts:
        return _parse_json_text("\n".join(texts))

    return _make_json_safe(result)


# -------------------------
# TOOL WRAPPER (FORCE JSON)
# -------------------------
def _wrap_tool(tool):
    async def _arun(**kwargs):
        raw = await tool.ainvoke(kwargs)
        normalized = _normalize_tool_result(raw)
        return json.dumps(normalized, ensure_ascii=False)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_arun,
    )


# -------------------------
# CONFIG LOADING
# -------------------------
def load_mcp_config():
    config_path = os.path.join(project_root, "config.json")

    with open(config_path, "r") as f:
        data = json.load(f)

    if "mcpServers" in data:
        data = data["mcpServers"]

    return data


# -------------------------
# TOOL LOADER
# -------------------------
async def get_tools_safe(all_servers):
    all_tools = []
    failed = []

    for name, cfg in all_servers.items():
        try:
            client = MultiServerMCPClient({name: cfg})
            tools = await client.get_tools()

            wrapped = [_wrap_tool(t) for t in tools]

            all_tools.extend(wrapped)
            print(f"✅ {name}: {len(wrapped)} tools")

        except Exception as e:
            print(f"❌ {name} failed: {e}")
            failed.append(name)

    return all_tools, failed


# -------------------------
# GLOBALS
# -------------------------
mcp_servers = {}
cached_tools = []
failed_servers = []


# -------------------------
# FASTAPI LIFESPAN
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_servers, cached_tools, failed_servers

    mcp_servers = load_mcp_config()
    cached_tools, failed_servers = await get_tools_safe(mcp_servers)

    yield


app = FastAPI(lifespan=lifespan)


# -------------------------
# REQUEST MODEL
# -------------------------
class QueryRequest(BaseModel):
    prompt: str
    thread_id: str = "default"


memory = MemorySaver()


# -------------------------
# CHAT ENDPOINT (CRITICAL FIX HERE)
# -------------------------
@app.post("/chat")
async def chat_endpoint(req: QueryRequest):

    if not cached_tools:
        raise HTTPException(status_code=500, detail="No tools available")

    try:
        llm = AzureChatOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            azure_deployment=os.getenv("AZURE_DEPLOYMENT_NAME")
        )

        agent = create_react_agent(llm, cached_tools, checkpointer=memory)

        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=req.prompt)]},
            {"configurable": {"thread_id": req.thread_id}}
        )

        messages = result.get("messages", [])

        # ✅ RETURN TOOL OUTPUT DIRECTLY
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]

        if tool_msgs:
            last = tool_msgs[-1]

            try:
                return {
                    "response": json.loads(last.content),
                    "source": "tool"
                }
            except:
                return {
                    "response": last.content,
                    "source": "tool"
                }

        # fallback
        return {
            "response": messages[-1].content,
            "source": "llm"
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------
# TOOLS ENDPOINT
# -------------------------
@app.get("/tools")
async def list_tools():
    return {
        "tools": [t.name for t in cached_tools],
        "failed": failed_servers
    }


# -------------------------
# RUN
# -------------------------
def start_gateway(host="0.0.0.0", port=8010):
    print(f"🚀 Starting MCP Gateway on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_gateway()
