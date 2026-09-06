import asyncio
from livekit import rtc
import urllib.request
import json
import time

async def main():
    print("Fetching token...")
    req = urllib.request.Request("http://localhost:8080/token")
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read().decode())
    
    import jwt
    decoded = jwt.decode(data["token"], options={"verify_signature": False})
    session_id = decoded["video"]["room"]
    print(f"SESSION_ID: {session_id}", flush=True)
    
    print(f"Connecting to room {session_id}...")
    room = rtc.Room()
    await room.connect(data["url"], data["token"])
    
    # Publish audio
    source = rtc.AudioSource(sample_rate=48000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    
    print("Publishing dummy audio for 8 seconds...")
    frame = rtc.AudioFrame.create(48000, 1, 480)
    for i in range(480): frame.data[i] = 0
    
    for _ in range(800): # 8 seconds (100 * 10ms = 1s)
        await source.capture_frame(frame)
        await asyncio.sleep(0.01)
        
    print("Disconnecting...")
    await room.disconnect()
    
    print("Waiting 5 seconds for worker to process on_disconnected...")
    await asyncio.sleep(5)
    
if __name__ == "__main__":
    asyncio.run(main())
