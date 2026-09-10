import asyncio
import httpx
import websockets
import json
import time
import os
from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv()

async def main():
    session_id = f"test-handoff-{int(time.time())}"
    
    # 1. Connect Caller to LiveKit
    token = api.AccessToken(
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET")
    ).with_identity(f"caller-{session_id}").with_name("Caller").with_grants(api.VideoGrants(
        room_join=True,
        room=session_id,
    )).to_jwt()
    
    room = rtc.Room()
    
    first_agent_speech_received = asyncio.Event()
    operator_speech_received = asyncio.Event()

    @room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            async def monitor_stream():
                stream = rtc.AudioStream(track)
                frame_count = 0
                async for event in stream:
                    frame_count += 1
                    if frame_count == 1:
                        first_agent_speech_received.set()
                    elif operator_speech_received.is_set() == False and first_agent_speech_received.is_set():
                        # We just want to log that audio is flowing.
                        pass
            asyncio.create_task(monitor_stream())
            
    print(f"[{time.time():.3f}] Caller: Connecting to room {session_id}...")
    await room.connect(os.getenv("LIVEKIT_URL"), token)
    
    # 2. Login to FastAPI Dashboard to get auth cookie
    print(f"[{time.time():.3f}] Dashboard: Logging in...")
    async with httpx.AsyncClient() as client:
        login_url = "http://localhost:8000/login"
        data = {"username": "admin", "password": "password"}
        resp = await client.post(login_url, data=data, follow_redirects=False)
        # Handle multiple Set-Cookie headers properly
        cookie_header = resp.headers.get_list("Set-Cookie")
        session_cookie = ""
        for c in cookie_header:
            if c.startswith("session="):
                session_cookie = c.split(";")[0]
                break
        
    print(f"[{time.time():.3f}] Dashboard: Connecting to WebSocket...")
    # 3. Connect to Dashboard WebSocket and send operator_message
    async with websockets.connect('ws://localhost:8000/ws/dashboard', additional_headers={"Cookie": session_cookie}) as ws:
        # Drain the snapshot
        await ws.recv()
        
        # Wait for agent to start up and send greeting
        print(f"[{time.time():.3f}] Caller: Waiting for greeting...")
        await first_agent_speech_received.wait()
        await asyncio.sleep(5) # Let greeting play
        
        # Trigger Escalation! We'll just send operator message immediately to test IPC
        operator_text = "Hello, this is a human dispatcher taking over."
        print(f"[{time.time():.3f}] Dashboard: Sending Human Takeover message via WebSocket: '{operator_text}'")
        
        await ws.send(json.dumps({
            "type": "operator_message",
            "session_id": session_id,
            "text": operator_text
        }))
        
        # Now we wait for the webhook to relay it to the worker, and the worker to synthesize and play it back
        print(f"[{time.time():.3f}] Caller: Listening for human operator audio synthesis...", flush=True)
        
        timeout = time.time() + 15
        while time.time() < timeout:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "transcript_chunk" and msg["data"].get("speaker") == "agent":
                spoken_text = msg["data"].get("text")
                if "human dispatcher" in spoken_text:
                    print(f"[{time.time():.3f}] SUCCESS: Dashboard confirmed worker is speaking: '{spoken_text}'")
                    break
                    
    await asyncio.sleep(2)
    print("Test finished.")
    await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
