import asyncio
import urllib.request
import json
import base64
import subprocess
import os
import websockets
from livekit import rtc

def decode_jwt(token):
    payload_b64 = token.split('.')[1]
    payload_b64 += '=' * (-len(payload_b64) % 4)
    return json.loads(base64.b64decode(payload_b64).decode('utf-8'))

async def main():
    print("Starting LiveKit worker...")
    worker = subprocess.Popen(
        ["python", "-m", "app.agents.worker", "start", "--dev"],
        env=os.environ
    )
    
    await asyncio.sleep(3)
    
    # Connect two callers
    print("Fetching tokens...")
    req1 = urllib.request.Request('http://localhost:8080/token')
    with urllib.request.urlopen(req1) as res:
        data1 = json.loads(res.read())
        
    req2 = urllib.request.Request('http://localhost:8080/token')
    with urllib.request.urlopen(req2) as res:
        data2 = json.loads(res.read())
        
    session_a = decode_jwt(data1["token"])['video']['room']
    session_b = decode_jwt(data2["token"])['video']['room']
    
    print(f"Call A: {session_a}")
    print(f"Call B: {session_b}")
    
    print("Connecting callers via LiveKit...")
    room_a = rtc.Room()
    await room_a.connect(data1["url"], data1["token"])
    
    room_b = rtc.Room()
    await room_b.connect(data2["url"], data2["token"])
    
    print("Waiting for agents to dispatch and register IPC...")
    await asyncio.sleep(5)
    
    print("Connecting Dashboard WebSocket...")
    async with websockets.connect('ws://localhost:8000/ws/dashboard') as ws:
        # Ignore initial history snapshot
        await ws.recv()
        
        print(f"Sending operator message to Call A ({session_a})...")
        await ws.send(json.dumps({
            "type": "operator_message",
            "session_id": session_a,
            "text": "OPERATOR MESSAGE TO CALL A"
        }))
        
        # We expect to receive an "ack" from FastAPI, then "agent_speech" broadcast
        call_a_spoke = False
        call_b_spoke = False
        
        try:
            async with asyncio.timeout(5.0):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") == "ack":
                        print("Received ACK from FastAPI.")
                    elif msg.get("type") == "agent_speech":
                        spoken_session = msg.get("session_id")
                        spoken_text = msg["data"].get("text")
                        print(f"Received agent_speech from session {spoken_session}: '{spoken_text}'")
                        if spoken_session == session_a and "OPERATOR MESSAGE" in spoken_text:
                            call_a_spoke = True
                        if spoken_session == session_b and "OPERATOR MESSAGE" in spoken_text:
                            call_b_spoke = True
        except asyncio.TimeoutError:
            print("Finished waiting for broadcasts.")
            
    print("\n--- RESULTS ---")
    print(f"Call A Spoke: {call_a_spoke}")
    print(f"Call B Spoke: {call_b_spoke}")
    
    if call_a_spoke and not call_b_spoke:
        print("PASS: Operator message was routed correctly under concurrency.")
    else:
        print("FAIL: Routing incorrect.")
        
    print("Cleaning up...")
    await room_a.disconnect()
    await room_b.disconnect()
    worker.terminate()
    worker.wait()

if __name__ == "__main__":
    asyncio.run(main())
