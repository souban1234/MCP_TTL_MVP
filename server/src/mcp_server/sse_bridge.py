"""
sse_bridge.py — Single-connection SSE ↔ stdio bridge for Chromosome MCP server.
One SSE connection handles both: receiving the endpoint AND all subsequent responses.
"""
import asyncio
import sys
import json
import httpx
from urllib.parse import urlparse


async def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: sse_bridge.py <sse_url> <api_key> [base_prefix]\n")
        sys.exit(1)

    sse_url     = sys.argv[1]
    api_key     = sys.argv[2]
    base_prefix = sys.argv[3] if len(sys.argv) > 3 else ""

    sse_headers = {
        "x-api-key": api_key,
        "Accept": "text/event-stream",
        "Cache-Control": "no-store",
    }
    post_headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    # Shared state between coroutines
    post_url_ready = asyncio.Event()
    post_url = None

    async with httpx.AsyncClient(timeout=None) as client:

        async def sse_reader():
            """
            Keep ONE SSE connection open forever.
            - First data: line → extract & fix endpoint URL
            - All subsequent data: lines → forward JSON to stdout
            """
            nonlocal post_url
            endpoint_received = False

            async with client.stream("GET", sse_url, headers=sse_headers) as sse:
                sse.raise_for_status()
                sys.stderr.write("[bridge] SSE connected\n")

                async for line in sse.aiter_lines():

                    # Skip empty lines and SSE comment/ping lines
                    if not line or line.startswith(":"):
                        continue

                    # Skip "event:" lines (we don't need them)
                    if line.startswith("event:"):
                        continue

                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()

                        if not endpoint_received:
                            # First data line = endpoint path — fix and store it
                            if base_prefix and not payload.startswith(base_prefix):
                                fixed_path = base_prefix + payload
                            else:
                                fixed_path = payload

                            parsed = urlparse(sse_url)
                            post_url = f"{parsed.scheme}://{parsed.netloc}{fixed_path}"
                            sys.stderr.write(f"[bridge] Endpoint → {post_url}\n")
                            endpoint_received = True
                            post_url_ready.set()  # unblock stdin_reader

                        else:
                            # All subsequent data lines = JSON-RPC responses
                            if payload:
                                try:
                                    json.loads(payload)  # verify it's valid JSON
                                    sys.stdout.write(payload + "\n")
                                    sys.stdout.flush()
                                    sys.stderr.write(f"[bridge] → stdout: {payload[:80]}...\n")
                                except json.JSONDecodeError:
                                    sys.stderr.write(f"[bridge] Non-JSON data skipped: {payload[:60]}\n")

        async def stdin_reader():
            """
            Wait for endpoint to be ready, then forward stdin JSON-RPC → POST.
            """
            # Wait until SSE gives us the endpoint
            await post_url_ready.wait()
            sys.stderr.write("[bridge] stdin_reader ready, listening for input...\n")

            loop = asyncio.get_event_loop()
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

            while True:
                line = await reader.readline()
                if not line:
                    sys.stderr.write("[bridge] stdin closed\n")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode())
                    method = payload.get("method", "?")
                    resp = await client.post(post_url, json=payload, headers=post_headers)
                    sys.stderr.write(f"[bridge] POST {method} → {resp.status_code}\n")
                except json.JSONDecodeError as e:
                    sys.stderr.write(f"[bridge] Bad JSON from stdin: {e}\n")
                except Exception as e:
                    sys.stderr.write(f"[bridge] POST error: {e}\n")

        # Run both concurrently — ONE SSE connection shared between them
        await asyncio.gather(sse_reader(), stdin_reader())


if __name__ == "__main__":
    asyncio.run(main())
