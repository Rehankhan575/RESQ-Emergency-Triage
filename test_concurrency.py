import asyncio
import urllib.request
import json
import base64
import time
import subprocess
import os

def decode_jwt(token):
    payload_b64 = token.split('.')[1]
    payload_b64 += '=' * (-len(payload_b64) % 4)
    return json.loads(base64.b64decode(payload_b64).decode('utf-8'))

async def main():
    print("Starting LiveKit worker...")
    worker = subprocess.Popen(
        ["python", "-m", "app.agents.worker", "start", "--dev"],
        env=os.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    # Wait for the dev server to boot
    time.sleep(3)
    
    print("Hitting /token twice in rapid succession...")
    req1 = urllib.request.Request('http://localhost:8080/token')
    with urllib.request.urlopen(req1) as res:
        data1 = json.loads(res.read())
        
    req2 = urllib.request.Request('http://localhost:8080/token')
    with urllib.request.urlopen(req2) as res:
        data2 = json.loads(res.read())
        
    jwt1 = decode_jwt(data1["token"])
    jwt2 = decode_jwt(data2["token"])
    
    print(f"Call 1 - Identity: {jwt1['sub']}, Room: {jwt1['video']['room']}")
    print(f"Call 2 - Identity: {jwt2['sub']}, Room: {jwt2['video']['room']}")
    
    # We will simulate connecting using livekit SDK for python
    # to test if they show up distinctly in the DB/Dashboard
    from livekit import rtc
    
    print("Connecting participant 1...")
    room1 = rtc.Room()
    await room1.connect(data1["url"], data1["token"])
    
    print("Connecting participant 2...")
    room2 = rtc.Room()
    await room2.connect(data2["url"], data2["token"])
    
    print("Waiting 3s for agents to be dispatched and webhooks sent...")
    await asyncio.sleep(3)
    
    print("Disconnecting participants...")
    await room1.disconnect()
    await room2.disconnect()
    
    worker.terminate()
    worker.wait()

    # Now verify the database (CallLogDB)
    import sqlite3
    conn = sqlite3.connect("triage.db")
    c = conn.cursor()
    c.execute("SELECT session_id, full_transcript FROM call_logs ORDER BY created_at DESC LIMIT 5")
    rows = c.fetchall()
    print("\nRecent rows in CallLogDB:")
    for row in rows:
        print(row)
    
if __name__ == "__main__":
    asyncio.run(main())
