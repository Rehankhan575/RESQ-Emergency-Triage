import asyncio
from livekit import rtc
import time
import os
from dotenv import load_dotenv
from livekit import api

load_dotenv()

async def test_clipping_run(run_id: int):
    print(f"\n--- Run {run_id} ---")
    session_id = f"test-clip-{run_id}"
    
    token = api.AccessToken(
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET")
    ).with_identity(f"caller-{session_id}").with_name("Caller").with_grants(api.VideoGrants(
        room_join=True,
        room=session_id,
    )).to_jwt()
    
    url = os.getenv("LIVEKIT_URL")
    
    room = rtc.Room()
    
    # We will track when we subscribe to the agent's audio
    agent_audio_subscribed = asyncio.Event()
    first_audio_frame_received_time = 0.0
    
    @room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        print(f"[{time.time():.3f}] Client: Subscribed to agent track {track.sid}")
        agent_audio_subscribed.set()
        
        # Monitor the stream to see when the first frame actually arrives
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            async def monitor_stream():
                nonlocal first_audio_frame_received_time
                stream = rtc.AudioStream(track)
                async for event in stream:
                    if first_audio_frame_received_time == 0.0:
                        first_audio_frame_received_time = time.time()
                        print(f"[{first_audio_frame_received_time:.3f}] Client: Received FIRST audio frame from agent.")
                        break
            asyncio.create_task(monitor_stream())

    print(f"[{time.time():.3f}] Client: Connecting to room...")
    await room.connect(url, token)
    print(f"[{time.time():.3f}] Client: Connected.")
    
    # Wait for the agent to say the greeting
    await asyncio.sleep(5)
    await room.disconnect()

async def main():
    for i in range(1, 6):
        await test_clipping_run(i)

if __name__ == "__main__":
    asyncio.run(main())
