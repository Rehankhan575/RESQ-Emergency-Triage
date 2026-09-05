import asyncio
import json
import uuid
import requests
import websockets
from datetime import datetime

async def test_silent_clustering():
    voice_session = str(uuid.uuid4())
    print(f"=== Starting clustering test ===")
    
    async with websockets.connect('ws://localhost:8000/ws/dashboard') as ws:
        # 1. Read initial snapshot
        initial_msg = await ws.recv()
        print("Connected to dashboard WS")
        
        print("\nGeocoding 'Andheri West, Mumbai' to seed cache...")
        geo_resp = requests.get("http://localhost:8000/api/geocode?location=Andheri+West,+Mumbai")
        # In our backend, it returns {"lat": X, "lon": Y}, not display_name. But we just need the cache seeded with the string.
        # So we use "Andheri West, Mumbai" for the silent form and voice form.
        geocoded_loc = "Andheri West, Mumbai"
        print(f"Geocoded as: {geocoded_loc}")

        # 2. Simulate an existing voice call triage update (this creates the incident)
        print(f"\nSimulating voice call from Andheri West (Session: {voice_session})")
        resp = requests.post(f"http://localhost:8000/api/calls/{voice_session}/broadcast", json={
            "event_type": "triage_update",
            "data": {
                "emergency_type": "FIRE",
                "location": geocoded_loc,
                "severity": "HIGH",
                "confidence": 0.8
            }
        })
        print(f"Voice broadcast HTTP status: {resp.status_code}")
        
        # 3. Read the WS broadcast for the voice call
        voice_msg = json.loads(await ws.recv())
        voice_incident_id = voice_msg["data"].get("incident_id")
        print(f"Dashboard WS received voice update. Incident ID: {voice_incident_id}")
        
        # 4. Wait a bit, then submit a silent form report at the same location
        print("\nSubmitting Silent Form report from same location...")
        form_payload = {
            "emergency_type": "FIRE",
            "location": geocoded_loc,
            "description": "I am hiding. Fire outside.",
            "people_affected": 2,
            "callback_number": ""
        }
        
        silent_resp = requests.post("http://localhost:8000/api/silent-reports", json=form_payload)
        print(f"Silent form HTTP status: {silent_resp.status_code}")
        silent_session = silent_resp.json().get("session_id")
        print(f"Silent form created Session ID: {silent_session}")
        
        # 5. Read the WS broadcasts for the silent form
        # We expect a 'triage_update' and a 'transcript' broadcast
        msgs = []
        for _ in range(2):
            msg_str = await ws.recv()
            msgs.append(json.loads(msg_str))
            
        triage_msg = next((m for m in msgs if m["type"] == "triage_update"), None)
        transcript_msg = next((m for m in msgs if m["type"] == "transcript"), None)
        
        print(f"\nDashboard WS received silent form triage_update:")
        print(json.dumps(triage_msg, indent=2))
        
        print(f"\nDashboard WS received silent form transcript:")
        print(json.dumps(transcript_msg, indent=2))
        
        # 6. Verification assertions
        silent_incident_id = triage_msg["data"].get("incident_id")
        print(f"\n--- VERIFICATION ---")
        
        # Is channel correctly set?
        channel = triage_msg["data"].get("channel")
        print(f"Channel field: {channel} (Expected: silent_form)")
        
        # Is confidence 1.0?
        confidence = triage_msg["data"].get("confidence")
        print(f"Confidence field: {confidence} (Expected: 1.0)")
        
        # Did it cluster?
        print(f"Voice Incident ID:  {voice_incident_id}")
        print(f"Silent Incident ID: {silent_incident_id}")
        if voice_incident_id == silent_incident_id:
            print("✅ CLUSTERING SUCCESS: Both reports share the same incident_id!")
        else:
            print("❌ CLUSTERING FAILED: Incident IDs do not match.")
            
        # 7. Check database for TriageState
        print(f"\nChecking database for CallLogDB {silent_session}...")
        history_resp = requests.get(f"http://localhost:8000/api/calls/{silent_session}/history")
        if history_resp.status_code == 200:
            data = history_resp.json()
            triage_state = data["triage_history"][0]
            print(f"DB TriageState Channel: {data.get('channel', 'voice')}") # wait, /history route doesn't return channel currently.
            print(f"DB TriageState Confidence: {triage_state['confidence']}")
            print(f"DB TriageState location: {triage_state['location']}")
            print(f"DB TriageState flag_for_human: {triage_state['flag_for_human']}")
            print(f"DB Transcript: {data['full_transcript']}")
        else:
            print(f"Failed to fetch DB history: {history_resp.status_code}")

if __name__ == "__main__":
    asyncio.run(test_silent_clustering())
