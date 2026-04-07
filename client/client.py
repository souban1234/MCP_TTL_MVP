# client/client.py - UPDATED VERSION WITH TATA TECHNOLOGIES INTEGRATION
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

# Project root resolution
if getattr(sys, 'frozen', False):
    project_root = os.path.dirname(sys.executable)
else:
    current_script_path = os.path.abspath(__file__)
    client_dir = os.path.dirname(current_script_path)
    project_root = os.path.abspath(os.path.join(client_dir, ".."))


def load_mcp_config():
    """Load MCP configuration from config.json"""
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

        if transport == "sse":
            url = settings.get("url")
            api_key = settings.get("api_key") or settings.get("headers", {}).get("x-api-key")
            prefix = settings.get("prefix", "")

            if not url or not api_key:
                print(f"⚠️ Skipping SSE server '{server_name}': URL or API key missing.")
                continue

            servers[server_name] = {
                "_transport": "sse_direct",
                "url": url,
                "api_key": api_key,
                "prefix": prefix,
            }
            print(f"🌉 SSE direct registered: {server_name} → {url}")
            
        elif transport == "stdio":
            custom_command = settings.get("command")
            custom_args = settings.get("args", [])
            custom_env = settings.get("env", {})

            resolved_env = {k: os.getenv(v, v) for k, v in custom_env.items()}
            full_env = {**os.environ, **resolved_env} if resolved_env else None

            if custom_command:
                executable = custom_command
                if custom_command == "python" or custom_command == "python3":
                    executable = sys.executable
                
                if not os.path.isabs(executable) and not executable.startswith(("uvx", "npx")):
                    candidate_path = os.path.abspath(os.path.join(project_root, executable))
                    if os.path.exists(candidate_path):
                        executable = candidate_path
                    elif os.path.exists(candidate_path + ".exe"):
                        executable = candidate_path + ".exe"
                
                server_config = {
                    "transport": "stdio",
                    "command": executable,
                    "args": custom_args,
                }
                if full_env:
                    server_config["env"] = full_env

                servers[server_name] = server_config
                print(f"🔧 stdio (custom cmd) registered: {server_name} → {executable}")
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
                print(f"🖥️ stdio server registered: {server_name} → {script_abs_path}")

    return servers


# ============================================================================
# SSE MCP CLIENT - FIXED VERSION FOR TATA TECHNOLOGIES SERVER
# ============================================================================

async def _sse_rpc(sse_url: str, api_key: str, prefix: str,
                   method: str, params: dict, timeout: float = 30.0) -> dict:
    """
    Execute ONE MCP method via SSE transport.
    
    Args:
        sse_url: SSE endpoint URL
        api_key: Authentication API key
        prefix: Path prefix for discovered POST URL
        method: MCP method name (e.g., 'tools/list')
        params: MCP method parameters
        timeout: Request timeout in seconds
    
    Returns:
        MCP result dictionary
    """
    sse_hdrs = {
        "x-api-key": api_key,
        "Accept": "text/event-stream",
        "Cache-Control": "no-store"
    }
    post_hdrs = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    q: asyncio.Queue = asyncio.Queue()
    ready = asyncio.Event()
    purl: list = [None]

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), verify=False) as http:

        async def _sse_reader():
            """Read SSE stream to discover POST endpoint"""
            async with http.stream("GET", sse_url, headers=sse_hdrs) as resp:
                resp.raise_for_status()
                async for ln in resp.aiter_lines():
                    if not ln or ln.startswith(":") or ln.startswith("event:"):
                        continue
                    if ln.startswith("data:"):
                        data = ln[5:].strip()
                        if purl[0] is None:
                            # ✅ FIXED: Handle prefix correctly
                            if prefix and data and not data.startswith(prefix):
                                path = prefix + data
                            else:
                                path = data
                            p = _urlparse(sse_url)
                            purl[0] = f"{p.scheme}://{p.netloc}{path}"
                            print(f"   ✅ POST endpoint discovered: {purl[0]}")
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

            # MCP initialize handshake
            print(f"   🤝 Initializing MCP protocol...")
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0"}
                }
            })
            await asyncio.wait_for(q.get(), timeout=timeout)
            print(f"   ✅ MCP initialized")

            # Notification: initialized
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {}
            })

            # Call the actual method
            print(f"   📞 Calling: {method}")
            await http.post(url, headers=post_hdrs, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params
            })
            
            result_msg = await asyncio.wait_for(q.get(), timeout=timeout)
            print(f"   ✅ Method '{method}' completed")
            return result_msg.get("result", {})
            
        finally:
            sse_task.cancel()
            try:
                await sse_task
            except asyncio.CancelledError:
                pass


def _make_tool_input_model(tool_name: str, schema: dict):
    """Build a Pydantic model from an MCP tool's inputSchema"""
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


async def get_sse_tools_direct(server_name: str, sse_url: str, api_key: str,
                                prefix: str = "", timeout: float = 30.0) -> list:
    """
    ✅ MAIN FUNCTION: Discover and wrap tools from Tata Technologies MCP server.
    
    This function:
    1. Connects to the SSE endpoint
    2. Discovers available tools
    3. Wraps each tool as a LangChain StructuredTool
    4. Returns ready-to-use tools for the LLM agent
    
    Args:
        server_name: Display name of the server
        sse_url: SSE endpoint (e.g., https://chromosome.tatatechnologies.com/mcp-server/mcp)
        api_key: Authentication API key
        prefix: Path prefix for POST endpoint (usually empty for direct URLs)
        timeout: Timeout in seconds
    
    Returns:
        List of LangChain StructuredTool objects
    """
    print(f"\n🔍 Discovering tools from {server_name}...")
    print(f"   Server URL: {sse_url}")
    print(f"   Prefix: '{prefix if prefix else '(none)'}'")
    
    # Step 1: Call tools/list to get available tools
    try:
        raw = await _sse_rpc(sse_url, api_key, prefix, "tools/list", {}, timeout=timeout)
    except Exception as e:
        print(f"   ❌ Failed to list tools: {e}")
        raise
    
    tool_defs = raw.get("tools", [])
    print(f"   ✅ Discovered {len(tool_defs)} tools")
    
    # Step 2: Create StructuredTool for each discovered tool
    lc_tools = []
    
    for t in tool_defs:
        name = t["name"]
        desc = t.get("description", "")
        schema = t.get("inputSchema", {})
        input_model = _make_tool_input_model(name, schema)
        
        print(f"      • {name}: {desc[:60]}...")
        
        # Capture closure variables explicitly
        _url, _key, _pfx, _tname = sse_url, api_key, prefix, name

        async def _arun(_n=_tname, _u=_url, _k=_key, _p=_pfx, **kwargs):
            """Async execution of the tool"""
            try:
                result = await _sse_rpc(_u, _k, _p, "tools/call",
                                        {"name": _n, "arguments": kwargs}, timeout=60.0)
                content = result.get("content", [])
                parts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
                return "\n".join(parts) if parts else str(result)
            except Exception as e:
                return f"Error calling tool {_n}: {str(e)}"

        lc_tools.append(StructuredTool(
            name=name,
            description=desc,
            args_schema=input_model,
            coroutine=_arun,
        ))

    print(f"   ✅ Wrapped {len(lc_tools)} tools for LLM")
    return lc_tools


async def get_tools_safe(all_servers: dict) -> tuple[list, list]:
    """
    Safely connect to all configured servers and load their tools.
    
    Returns:
        (all_tools: list, failed_servers: list)
    """
    all_tools = []
    failed_servers = []

    for server_name, server_config in all_servers.items():
        try:
            print(f"\n🔌 Connecting to '{server_name}'...")

            if server_config.get("_transport") == "sse_direct":
                # ✅ TATA TECHNOLOGIES: Use in-process SSE client
                tools = await asyncio.wait_for(
                    get_sse_tools_direct(
                        server_name,
                        server_config["url"],
                        server_config["api_key"],
                        server_config.get("prefix", ""),
                    ),
                    timeout=60.0,
                )
            else:
                # Standard stdio transport
                client = MultiServerMCPClient({server_name: server_config})
                tools = await asyncio.wait_for(client.get_tools(), timeout=60.0)

            all_tools.extend(tools)
            print(f"✅ Loaded {len(tools)} tools from '{server_name}'")
            
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout connecting to '{server_name}' — skipping.")
            failed_servers.append(server_name)
        except Exception as e:
            print(f"❌ Failed to load tools from '{server_name}': {e}")
            traceback.print_exc()
            failed_servers.append(server_name)

    return all_tools, failed_servers


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

mcp_servers = {}
cached_tools = []
failed_servers = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler - runs at startup and shutdown"""
    global mcp_servers, cached_tools, failed_servers
    try:
        print("\n" + "="*70)
        print("🚀 STARTUP: MCP Gateway Initialization")
        print("="*70)
        
        mcp_servers = load_mcp_config()
        print(f"\n📊 Servers configured: {list(mcp_servers.keys())}")
        
        # Discover and cache tools at startup
        print(f"\n🔍 Initializing MCP tools...")
        cached_tools, failed_servers = await get_tools_safe(mcp_servers)
        
        if failed_servers:
            print(f"\n⚠️ Failed servers: {failed_servers}")
        
        print(f"\n✅ Startup complete.")
        print(f"   Total tools: {len(cached_tools)}")
        print(f"   Tool names: {[t.name for t in cached_tools]}")
        print("="*70 + "\n")
        
        yield
    except Exception as e:
        print(f"❌ Error during startup: {e}")
        traceback.print_exc()
        raise
    finally:
        print("\n🔌 Shutting down MCP Gateway...")


app = FastAPI(lifespan=lifespan)


class QueryRequest(BaseModel):
    prompt: str
    thread_id: str = "default_thread"


# Initialize in-memory checkpointer for conversation history
memory = MemorySaver()


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.post("/chat")
async def chat_endpoint(request: QueryRequest):
    """
    ✅ MAIN CHAT ENDPOINT
    
    Processes user query by:
    1. Checking available tools
    2. Creating LLM agent
    3. Agent calls tools as needed
    4. Returns final response
    """
    global mcp_servers, cached_tools, failed_servers

    if not mcp_servers:
        raise HTTPException(status_code=500, detail="No MCP servers configured")

    try:
        tools = cached_tools
        failed = failed_servers

        if not tools:
            if failed:
                raise HTTPException(
                    status_code=503,
                    detail=f"No tools available. Failed servers: {', '.join(failed)}"
                )
            else:
                raise HTTPException(status_code=503, detail="No tools available.")

        print(f"\n📊 Chat Request Received")
        print(f"   Prompt: {request.prompt[:80]}...")
        print(f"   Available tools: {len(tools)}")
        print(f"   Tool names: {[t.name for t in tools]}")

        # Initialize LLM
        llm = AzureChatOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            azure_deployment=os.getenv("AZURE_DEPLOYMENT_NAME")
        )

        # Create agent with conversation memory
        agent = create_react_agent(llm, tools, checkpointer=memory)
        
        # Configure thread for multi-turn conversation
        config = {"configurable": {"thread_id": request.thread_id}}
        
        # Invoke the agent
        print(f"🤖 Invoking agent...")
        inputs = {"messages": [HumanMessage(content=request.prompt)]}
        result = await agent.ainvoke(inputs, config=config)
        
        # Extract final response
        final_message = result["messages"][-1]
        result_content = final_message.content
        
        print(f"✅ Agent completed successfully")
        
        if failed:
            result_content += f"\n\n⚠️ Note: Some servers were unavailable: {', '.join(failed)}"
            
        return {
            "response": result_content,
            "tools_available": len(tools),
            "failed_servers": failed
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"\n❌ Exception in chat_endpoint: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/tools")
async def list_tools():
    """Get list of all available tools"""
    return {
        "total": len(cached_tools),
        "tools": [
            {
                "name": t.name,
                "description": t.description or "No description"
            }
            for t in cached_tools
        ],
        "failed_servers": failed_servers
    }


@app.get("/servers/status")
async def servers_status():
    """Get status of all configured servers and their tools"""
    global mcp_servers, cached_tools, failed_servers
    
    return {
        "total_servers": len(mcp_servers),
        "configured_servers": list(mcp_servers.keys()),
        "tools_loaded": len(cached_tools),
        "failed_servers": failed_servers,
        "tools": [
            {
                "name": t.name,
                "description": (t.description or "")[:100]
            }
            for t in cached_tools
        ]
    }


@app.post("/tools/call")
async def call_tool_directly(tool_name: str, tool_args: dict):
    """
    Directly call a specific tool without using the agent.
    
    Useful for testing tools or direct integration.
    """
    global cached_tools
    
    try:
        tool = next((t for t in cached_tools if t.name == tool_name), None)
        if not tool:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found. Available: {[t.name for t in cached_tools]}"
            )
        
        print(f"\n📞 Directly calling tool: {tool_name}")
        print(f"    Args: {tool_args}")
        
        # Call the tool coroutine
        result = await tool.coroutine(**tool_args)
        
        print(f"✅ Tool result: {result[:100]}...")
        
        return {
            "tool": tool_name,
            "result": result
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reconnect")
async def reconnect_servers():
    """
    Reconnect to all configured servers.
    
    Useful if servers go down and come back online.
    """
    global mcp_servers, cached_tools, failed_servers
    
    try:
        print("\n🔄 Reconnecting to all servers...")
        cached_tools, failed_servers = await get_tools_safe(mcp_servers)
        
        return {
            "status": "reconnected",
            "tools_loaded": len(cached_tools),
            "failed_servers": failed_servers
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "servers_configured": len(mcp_servers),
        "tools_loaded": len(cached_tools)
    }


def start_gateway(host="0.0.0.0", port=8010):
    """Start the FastAPI gateway server"""
    print(f"\n🚀 Starting MCP Gateway on {host}:{port}...")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_gateway()
