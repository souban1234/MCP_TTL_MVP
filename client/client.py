# client/client.py
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
import ssl
import httpx

# ==============================================================================
# THE ULTIMATE SSL BYPASS (Corporate VPN/Proxy Fix)
# ==============================================================================
os.environ['NODE_TLS_REJECT_UNAUTHORIZED'] = '0'
os.environ['PYTHONHTTPSVERIFY'] = '0'
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''
ssl._create_default_https_context = ssl._create_unverified_context

# Monkeypatch HTTPX to ALWAYS disable SSL verification globally!
# This fixes the MCP SDK "unhandled errors in a TaskGroup" caused by SSL drops.
_original_async_client_init = httpx.AsyncClient.__init__
_original_client_init = httpx.Client.__init__

def _patched_async_client_init(self, *args, **kwargs):
    kwargs['verify'] = False
    _original_async_client_init(self, *args, **kwargs)

def _patched_client_init(self, *args, **kwargs):
    kwargs['verify'] = False
    _original_client_init(self, *args, **kwargs)

httpx.AsyncClient.__init__ = _patched_async_client_init
httpx.Client.__init__ = _patched_client_init
# ==============================================================================

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import Field, create_model
from typing import Any
from urllib.parse import urlparse as _urlparse

load_dotenv()

if getattr(sys, 'frozen', False):
    project_root = os.path.dirname(sys.executable)
else:
    current_script_path = os.path.abspath(__file__)
    client_dir = os.path.dirname(current_script_path)
    project_root = os.path.abspath(os.path.join(client_dir, ".."))

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_mcp_config():
    config_path = os.path.join(project_root, "config.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found at: {config_path}")

    print(f"📂 Reading config from: {config_path}")
    with open(config_path, "r") as f:
        config_data = json.load(f)

    if "mcpServers" in config_data:
        config_data = config_data["mcpServers"]

    servers = {}

    for server_name, settings in config_data.items():
        transport = settings.get("transport", "stdio")

        # ── NATIVE MCP HTTP/SSE (Handled natively by LangChain) ───────────
        if transport in ["streamable_http", "fastmcp_http", "mcp_http"]:
            url = settings.get("url")
            if not url:
                continue
            
            # FastMCP servers expose SSE at /sse. If you forgot it, we append it automatically.
            if not url.endswith("/sse"):
                url = url.rstrip("/") + "/sse"
                
            server_cfg = {"transport": "sse", "url": url}
            
            headers = settings.get("headers", {})
            api_key = settings.get("api_key")
            if api_key:
                headers["x-api-key"] = api_key
            if headers:
                server_cfg["headers"] = headers
                
            servers[server_name] = server_cfg
            print(f"🌐 Native MCP Remote registered: {server_name} → {url}")

        # ── LEGACY CUSTOM SSE (With Prefix Support) ───────────────────────
        elif transport == "sse":
            url = settings.get("url")
            api_key = settings.get("api_key") or settings.get("headers", {}).get("x-api-key")
            prefix = settings.get("prefix", "")
            
            if not url:
                continue
                
            servers[server_name] = {
                "_transport": "sse_legacy",
                "url": url,
                "api_key": api_key, # None is OK!
                "prefix": prefix,
            }
            print(f"🌉 Legacy SSE registered: {server_name} → {url}")

        # ── STDIO ─────────────────────────────────────────────────────────
        elif transport == "stdio":
            custom_command = settings.get("command")
            custom_args = settings.get("args", [])
            custom_env = settings.get("env", {})

            resolved_env = {k: os.getenv(v, v) for k, v in custom_env.items()}
            full_env = {**os.environ, **resolved_env} if resolved_env else None

            if custom_command:
                executable = custom_command
                if custom_command in ("python", "python3"):
                    executable = sys.executable
                if not os.path.isabs(executable) and not executable.startswith(("uvx", "npx")):
                    candidate = os.path.abspath(os.path.join(project_root, executable))
                    if os.path.exists(candidate):
                        executable = candidate
                    elif os.path.exists(candidate + ".exe"):
                        executable = candidate + ".exe"
                server_cfg = {
                    "transport": "stdio",
                    "command": executable,
                    "args": custom_args,
                }
                if full_env:
                    server_cfg["env"] = full_env
                servers[server_name] = server_cfg
                print(f"🔧 stdio registered: {server_name} → {executable}")
            else:
                script_rel_path = settings.get("script_path")
                if not script_rel_path:
                    continue
                script_abs_path = os.path.join(project_root, script_rel_path)
                servers[server_name] = {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [script_abs_path],
                }
                print(f"🖥️  stdio registered: {server_name} → {script_abs_path}")

    return servers


# ---------------------------------------------------------------------------
# Pydantic model builder
# ---------------------------------------------------------------------------

def _make_tool_input_model(tool_name: str, schema: dict):
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    fields: dict = {}
    for pname, pschema in properties.items():
        ptype_str = pschema.get("type", "string")
        ptype: Any = str
        if ptype_str in ("number", "integer"):
            ptype = float
        elif ptype_str == "boolean":
            ptype = bool
        elif ptype_str == "array":
            ptype = list
        elif ptype_str == "object":
            ptype = dict
        default = ... if pname in required_fields else None
        desc = pschema.get("description", pname)
        fields[pname] = (ptype, Field(default, description=desc))
    if not fields:
        return type("EmptyInput", (object,), {"__annotations__": {}})
    return create_model(f"{tool_name}Input", **fields)


# ---------------------------------------------------------------------------
# In-process SSE client (Legacy)
# ---------------------------------------------------------------------------

async def _sse_rpc(sse_url, api_key, prefix, method, params, timeout=30.0):
    sse_hdrs  = {"Accept": "text/event-stream", "Cache-Control": "no-store"}
    post_hdrs = {"Content-Type": "application/json"}
    
    if api_key:
        sse_hdrs["x-api-key"] = api_key
        post_hdrs["x-api-key"] = api_key
        
    q     = asyncio.Queue()
    ready = asyncio.Event()
    purl  = [None]

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as http:

        async def _sse_reader():
            async with http.stream("GET", sse_url, headers=sse_hdrs) as resp:
                resp.raise_for_status()
                async for ln in resp.aiter_lines():
                    if not ln or ln.startswith(":") or ln.startswith("event:"):
                        continue
                    if ln.startswith("data:"):
                        data = ln[5:].strip()
                        if purl[0] is None:
                            path = (prefix + data) if prefix and not data.startswith(prefix) else data
                            p = _urlparse(sse_url)
                            purl[0] = f"{p.scheme}://{p.netloc}{path}"
                            ready.set()
                        else:
                            try:
                                await q.put(json.loads(data))
                            except Exception:
                                pass

        sse_task = asyncio.ensure_future(_sse_reader())
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
            url = purl[0]
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "mcp-gateway", "version": "1.0"}}
            })
            await asyncio.wait_for(q.get(), timeout=timeout)
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
            })
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params
            })
            result_msg = await asyncio.wait_for(q.get(), timeout=timeout)
            return result_msg.get("result", {})
        finally:
            sse_task.cancel()
            try:
                await sse_task
            except asyncio.CancelledError:
                pass

async def get_sse_tools_direct(server_name, sse_url, api_key, prefix="", timeout=30.0):
    raw      = await _sse_rpc(sse_url, api_key, prefix, "tools/list", {}, timeout=timeout)
    tool_defs = raw.get("tools", [])
    lc_tools  = []
    for t in tool_defs:
        name        = t["name"]
        desc        = t.get("description", "")
        schema      = t.get("inputSchema", {})
        input_model = _make_tool_input_model(name, schema)
        _url, _key, _pfx, _tname = sse_url, api_key, prefix, name

        async def _arun(_n=_tname, _u=_url, _k=_key, _p=_pfx, **kwargs):
            result  = await _sse_rpc(_u, _k, _p, "tools/call",
                                     {"name": _n, "arguments": kwargs}, timeout=60.0)
            content = result.get("content", [])
            parts   = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
            return "\n".join(parts) if parts else str(result)

        lc_tools.append(StructuredTool(
            name=name, description=desc,
            args_schema=input_model, coroutine=_arun,
        ))
    return lc_tools


# ---------------------------------------------------------------------------
# Unified tool discovery
# ---------------------------------------------------------------------------

async def get_tools_safe(all_servers: dict) -> tuple[list, list]:
    all_tools      = []
    failed_servers = []

    for server_name, server_config in all_servers.items():
        try:
            print(f"🔌 Connecting to '{server_name}'...")
            transport = server_config.get("_transport") or server_config.get("transport")

            if transport == "sse_legacy":
                tools = await asyncio.wait_for(
                    get_sse_tools_direct(
                        server_name,
                        server_config["url"],
                        server_config.get("api_key"),
                        server_config.get("prefix", ""),
                    ),
                    timeout=30.0,
                )
            else:
                # Native Langchain MultiServerMCPClient handles "sse" seamlessly 
                client = MultiServerMCPClient({server_name: server_config})
                tools  = await asyncio.wait_for(client.get_tools(), timeout=60.0)

            all_tools.extend(tools)
            print(f"✅ Loaded {len(tools)} tools from '{server_name}'")

        except asyncio.TimeoutError:
            print(f"⏱️  Timeout connecting to '{server_name}' — skipping.")
            failed_servers.append(server_name)
        except Exception as e:
            print(f"❌ Failed to load tools from '{server_name}': {e}")
            traceback.print_exc()
            failed_servers.append(server_name)

    return all_tools, failed_servers


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

mcp_servers    = {}
cached_tools   = []
failed_servers = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_servers, cached_tools, failed_servers
    try:
        mcp_servers = load_mcp_config()
        print(f"🔌 Servers configured: {list(mcp_servers.keys())}")
        print("🔍 Initializing MCP tools...")
        cached_tools, failed_servers = await get_tools_safe(mcp_servers)
        if failed_servers:
            print(f"⚠️  Some servers failed at startup: {failed_servers}")
        print(f"✅ Startup complete. {len(cached_tools)} tools cached.")
        yield
    except Exception as e:
        print(f"❌ Error during startup: {e}")
        raise
    finally:
        print("🔌 Shutting down...")

app = FastAPI(lifespan=lifespan)

class QueryRequest(BaseModel):
    prompt: str
    thread_id: str = "default_thread"

memory = MemorySaver()

@app.post("/chat")
async def chat_endpoint(request: QueryRequest):
    global mcp_servers, cached_tools, failed_servers

    if not mcp_servers:
        raise HTTPException(status_code=500, detail="No MCP servers configured")

    try:
        tools  = cached_tools
        failed = failed_servers

        if not tools:
            raise HTTPException(
                status_code=503,
                detail=f"No tools available. Failed servers: {failed}"
            )

        llm = AzureChatOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            azure_deployment=os.getenv("AZURE_DEPLOYMENT_NAME"),
        )

        agent  = create_react_agent(llm, tools, checkpointer=memory)
        config = {"configurable": {"thread_id": request.thread_id}}
        inputs = {"messages": [HumanMessage(content=request.prompt)]}
        result = await agent.ainvoke(inputs, config=config)

        result_content = result["messages"][-1].content
        if failed:
            result_content += f"\n\n⚠️ Note: These servers were unavailable: {failed}"

        return {"response": result_content}

    except HTTPException:
        raise
    except Exception as e:
        print("❌ Exception in chat_endpoint:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tools")
async def list_tools():
    return {
        "loaded": [t.name for t in cached_tools],
        "failed_servers": failed_servers,
    }

def start_gateway(host="0.0.0.0", port=8010):
    print(f"🚀 Starting MCP Gateway on {host}:{port}...")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    start_gateway()
