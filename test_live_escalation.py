import asyncio
import os
import subprocess
import urllib.request
import json
import base64
import time
import re
import websockets
from livekit import rtc
import wave
import sys

def decode_jwt(token):
    payload_b64 = token.split('.')[1]
    payload_b64 += '=' * (-len(payload_b64) % 4)
    return json.loads(base64.b64decode(payload_b64).decode('utf-8'))

async def publish_audio(room: rtc.Room, file_path: str):
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("test-audio", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    
    wf = wave.open(file_path, 'rb')
    data = wf.readframes(wf.getnframes())
    
    frame_size = 320
    for i in range(0, len(data), frame_size):
        chunk = data[i:i+frame_size]
        if len(chunk) < frame_size:
            chunk += b'\0' * (frame_size - len(chunk))
        frame = rtc.AudioFrame(chunk, 16000, 1, len(chunk) // 2)
        await source.capture_frame(frame)
        await asyncio.sleep(0.01)

async def main():
    print("Starting LiveKit worker with INVALID Gemini Key to force LLM exceptions...")
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = "AIzaSy_invalid_key_for_testing_escalation"
    env["PYTHONUNBUFFERED"] = "1"
    
    worker = subprocess.Popen(
        [".venv/bin/python", "-m", "app.agents.worker", "start", "--dev"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    target_session = ""
    
    async def drain_stdout():
        while True:
            line = await asyncio.to_thread(worker.stdout.readline)
            if not line:
                break
            if 'Bypass LLM' in line or 'saying:' in line or 'OPERATOR MESSAGE' in line:
                print("[WORKER IPC MATCH] " + line.strip())
            elif '400' in line or 'API key not valid' in line or 'consecutive' in line or 'escalat' in line.lower():
                print("[WORKER GENUINE FAILURE] " + line.strip())
            elif 'Operator message for' in line or (target_session and target_session in line):
                if 'Operator' in line or 'Bypass' in line:
                    print("[WORKER TARGET MATCH] " + line.strip())

    asyncio.create_task(drain_stdout())
    
    await asyncio.sleep(4)
    
    import requests
    sessions = []
    for _ in range(3):
        req = urllib.request.Request('http://localhost:8080/token')
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read())
            sess_id = decode_jwt(data["token"])['video']['room']
            sessions.append((sess_id, data))
            
    for s_id, _ in sessions:
        requests.post(f"http://localhost:8000/api/calls/{s_id}/broadcast", json={
            "event_type": "triage_update",
            "data": {"emergency_type": "FIRE", "location": "Andheri West, Mumbai", "severity": "HIGH"}
        })
        
    print(f"Created 3 sessions: {[s[0] for s in sessions]}")
    target_session = sessions[0][0]
    
    print("Connecting callers via LiveKit...")
    rooms = []
    for i, (s_id, data) in enumerate(sessions):
        room = rtc.Room()
        await room.connect(data["url"], data["token"])
        rooms.append(room)
        
    await asyncio.sleep(4)
    target_room = rooms[0]
    
    print(f"--- FORCING ESCALATION ON CALL 1 ({target_session}) ---")
    print("Pushing audio turn 1 (will fail due to invalid Gemini key)...")
    await publish_audio(target_room, "test_audio.wav")
    
    await asyncio.sleep(6)
    
    print("Pushing audio turn 2 (will trigger escalation)...")
    await publish_audio(target_room, "test_audio.wav")
    
    await asyncio.sleep(6)
    
    print("--- SENDING OPERATOR MESSAGE TO CALL 1 ---")
    async with websockets.connect('ws://localhost:8000/ws/dashboard') as ws:
        await ws.recv() # initial snapshot
        await ws.send(json.dumps({
            "type": "operator_message",
            "session_id": target_session,
            "text": "OPERATOR MESSAGE TO SPECIFIC CLUSTERED CALLER"
        }))
        
    await asyncio.sleep(2)
    worker.terminate()
    print("\n--- TEST FINISHED ---")

if __name__ == "__main__":
    asyncio.run(main())
