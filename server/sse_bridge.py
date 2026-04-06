# server/sse_bridge.py
import asyncio
import sys
import json
import httpx
from urllib.parse import urlparse

async def main_bridge(sse_url, api_key="", base_prefix=""):
    sse_headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-store",
    }
    post_headers = {
        "Content-Type": "application/json",
    }

    # Only attach API keys if one was actually provided
    if api_key:
        sse_headers["x-api-key"] = api_key
        post_headers["x-api-key"] = api_key

    post_url_ready = asyncio.Event()
    post_url = None
    msg_queue: asyncio.Queue = asyncio.Queue()

    async with httpx.AsyncClient(timeout=None, verify=False) as client:

        async def sse_reader():
            nonlocal post_url
            endpoint_received = False

            async with client.stream("GET", sse_url, headers=sse_headers) as sse:
                sse.raise_for_status()
                sys.stderr.write("[bridge] SSE connected\n")

                async for line in sse.aiter_lines():
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue

                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()

                        if not endpoint_received:
                            fixed_path = (base_prefix + payload) if (base_prefix and not payload.startswith(base_prefix)) else payload
                            parsed = urlparse(sse_url)
                            post_url = f"{parsed.scheme}://{parsed.netloc}{fixed_path}"
                            sys.stderr.write(f"[bridge] Endpoint → {post_url}\n")
                            endpoint_received = True
                            post_url_ready.set()

                        else:
                            if payload:
                                try:
                                    json.loads(payload)
                                    sys.stdout.buffer.write((payload + "\n").encode('utf-8'))
                                    sys.stdout.buffer.flush()
                                except json.JSONDecodeError:
                                    sys.stderr.write(f"[bridge] Non-JSON data skipped: {payload[:60]}\n")

        async def stdin_reader():
            loop = asyncio.get_event_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
                if not line:
                    await msg_queue.put(None)
                    break

                cleaned = line.strip().decode('utf-8')
                if cleaned:
                    await msg_queue.put(cleaned)

        async def message_sender():
            await post_url_ready.wait()
            while True:
                msg = await msg_queue.get()
                if msg is None:
                    break
                try:
                    payload = json.loads(msg)
                    method = payload.get("method", "?")
                    resp = await client.post(post_url, json=payload, headers=post_headers)
                    sys.stderr.write(f"[bridge] POST {method} → {resp.status_code}\n")
                except json.JSONDecodeError as e:
                    sys.stderr.write(f"[bridge] Bad JSON from stdin: {e}\n")
                except Exception as e:
                    sys.stderr.write(f"[bridge] POST error: {e}\n")

        await asyncio.gather(sse_reader(), stdin_reader(), message_sender())

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: sse_bridge.py <sse_url> [api_key] [base_prefix]\n")
        sys.exit(1)

    u = sys.argv[1]
    k = sys.argv[2] if len(sys.argv) > 2 else ""
    p = sys.argv[3] if len(sys.argv) > 3 else ""
    
    asyncio.run(main_bridge(u, k, p))
