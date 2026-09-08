import asyncio
from livekit import rtc
import urllib.request
import json
import time
import wave

async def main():
    import uuid
    import os
    from dotenv import load_dotenv
    from livekit import api
    load_dotenv()
    
    session_id = str(uuid.uuid4())
    token = api.AccessToken(
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET")
    ).with_identity(f"caller-{session_id[:8]}").with_name("Caller").with_grants(api.VideoGrants(
        room_join=True,
        room=session_id,
    )).to_jwt()
    data = {"url": os.getenv("LIVEKIT_URL"), "token": token}
    
    import jwt
    decoded = jwt.decode(data["token"], options={"verify_signature": False})
    session_id = decoded["video"]["room"]
    print(f"SESSION_ID: {session_id}", flush=True)
    
    print(f"Connecting to room {session_id}...")
    room = rtc.Room()
    await room.connect(data["url"], data["token"])
    
    print("Waiting 6 seconds for the greeting to finish playing...")
    await asyncio.sleep(6)
    
    print("Publishing real audio for STT...")
    with wave.open("dummy.wav", "rb") as wf:
        raw_pcm = wf.readframes(wf.getnframes())
        sample_rate = wf.getframerate()
        num_channels = wf.getnchannels()

    # Publish audio using wav params
    source = rtc.AudioSource(sample_rate=sample_rate, num_channels=num_channels)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    
    chunk_duration_ms = 10
    samples_per_chunk = int(sample_rate * (chunk_duration_ms / 1000))
    bytes_per_sample = 2 * num_channels
    bytes_per_chunk = samples_per_chunk * bytes_per_sample
    
    print(f"Publishing audio in {chunk_duration_ms}ms chunks...")
    for i in range(0, len(raw_pcm), bytes_per_chunk):
        chunk_data = raw_pcm[i:i+bytes_per_chunk]
        if len(chunk_data) < bytes_per_chunk:
            break
        frame = rtc.AudioFrame(
            data=chunk_data,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=samples_per_chunk,
        )
        await source.capture_frame(frame)
        await asyncio.sleep(chunk_duration_ms / 1000)
        
    print("Publishing silence for 5 seconds to trigger STT FINAL_TRANSCRIPT...")
    silence_frame = rtc.AudioFrame(
        data=bytearray(bytes_per_chunk),
        sample_rate=sample_rate,
        num_channels=num_channels,
        samples_per_channel=samples_per_chunk,
    )
    for _ in range(500): # 500 * 10ms = 5s
        await source.capture_frame(silence_frame)
        await asyncio.sleep(chunk_duration_ms / 1000)
        
    print("Disconnecting...")
    await room.disconnect()
    
    print("Waiting 5 seconds for worker to process on_disconnected...")
    await asyncio.sleep(5)
    
if __name__ == "__main__":
    asyncio.run(main())
