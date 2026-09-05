import os
import sys
import json
import logging
import asyncio
import http.server
import socketserver
import threading
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.api import AccessToken, VideoGrants

# Load environment variables first
load_dotenv()

from app.agents.voice_agent import TriageVoiceAgent
from app.agents.triage.agent import warm_up_gemini_connection
from aiohttp import web
import httpx

logger = logging.getLogger("worker")

active_agents: dict[str, TriageVoiceAgent] = {}

async def handle_operator_message(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        session_id = data.get("session_id")
        text = data.get("text")
        
        agent = active_agents.get(session_id)
        if agent:
            # Bypass LLM completely, just speak it
            asyncio.create_task(agent.say(text))
            return web.json_response({"status": "ok"})
        else:
            logger.warning(f"Operator message for unknown session: {session_id}")
            return web.json_response({"error": "Agent not found"}, status=404)
    except Exception as e:
        logger.error(f"Error handling operator message: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def start_ipc_server(session_id: str):
    """Starts a dynamic-port HTTP server for this worker subprocess and registers it."""
    app = web.Application()
    app.router.add_post("/operator_message", handle_operator_message)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # port=0 asks OS to assign a random free port
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    
    # Retrieve the assigned port
    assigned_port = site._server.sockets[0].getsockname()[1]
    worker_url = f"http://127.0.0.1:{assigned_port}/operator_message"
    logger.info(f"Worker IPC server running at {worker_url}")
    
    # Register this URL with FastAPI
    fastapi_url = f"http://localhost:8000/api/calls/{session_id}/register_worker"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(fastapi_url, json={"worker_url": worker_url})
            logger.info(f"Successfully registered worker URL for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to register worker URL with FastAPI: {e}")


async def agent_entrypoint(ctx: JobContext):
    logger.info("Agent starting, connecting to room...")
    # Fire Gemini connection warm-up in the background — races in parallel with
    # LiveKit room setup so no real caller turn pays the cold-connection tax.
    asyncio.create_task(warm_up_gemini_connection())

    # Auto-subscribe strictly to AUDIO_ONLY so we can hear users
    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)

    agent = TriageVoiceAgent(ctx, session_id=ctx.room.name)
    
    # Register globally for IPC
    active_agents[ctx.room.name] = agent
    
    # Start IPC server dynamically
    asyncio.create_task(start_ipc_server(ctx.room.name))
    
    await agent.start()
    
    # Cleanup on disconnect to prevent memory leaks
    @ctx.room.on("disconnected")
    def on_disconnected():
        logger.info(f"Cleaning up agent for {ctx.room.name}")
        active_agents.pop(ctx.room.name, None)

def serve_dev_html():
    """Serves the dev index.html and generates a livekit token for local testing."""
    port = 8080
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    
    class DevHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                with open(html_path, 'rb') as f:
                    self.wfile.write(f.read())
            elif self.path == '/token':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                import uuid
                
                url = os.getenv("LIVEKIT_URL")
                api_key = os.getenv("LIVEKIT_API_KEY")
                api_secret = os.getenv("LIVEKIT_API_SECRET")
                
                # Generate a UNIQUE room and identity for every caller so they don't collide
                session_id = f"call-{uuid.uuid4().hex[:8]}"
                caller_identity = f"caller-{uuid.uuid4().hex[:8]}"
                
                token = AccessToken(api_key, api_secret) \
                    .with_identity(caller_identity) \
                    .with_name("Emergency Caller") \
                    .with_grants(VideoGrants(
                        room_join=True,
                        room=session_id
                    ))
                
                self.wfile.write(json.dumps({"url": url, "token": token.to_jwt()}).encode())
            else:
                super().do_GET()
                
    def run_server():
        with socketserver.TCPServer(("", port), DevHandler) as httpd:
            logger.info(f"Dev HTML server running at http://localhost:{port}")
            httpd.serve_forever()
            
    threading.Thread(target=run_server, daemon=True).start()

def main():
    if "--dev" in sys.argv:
        sys.argv.remove("--dev")
        serve_dev_html()
        
    cli.run_app(WorkerOptions(entrypoint_fnc=agent_entrypoint))

if __name__ == "__main__":
    main()
