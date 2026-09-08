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
    
    print("Generating simulated live-mic audio with a 1.2s pause...")
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))
    from app.agents.voice_agent import synthesize_tts
    
    part1 = await synthesize_tts("Mera naam Rahul hai,")
    part2 = await synthesize_tts("aur mujhe accident hua hai.")
    
    # Extract raw PCM (skip 44-byte WAV header)
    pcm1 = part1[44:]
    pcm2 = part2[44:]
    
    # 8000 Hz, 16-bit, 1 channel -> 16000 bytes/sec. 1.2 seconds = 19200 bytes of zeros
    silence = bytearray(19200)
    
    sample_rate = 8000
    num_channels = 1
    
    # Publish audio using wav params
    source = rtc.AudioSource(sample_rate=sample_rate, num_channels=num_channels)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    
    chunk_duration_ms = 10
    samples_per_chunk = int(sample_rate * (chunk_duration_ms / 1000))
    bytes_per_sample = 2 * num_channels
    bytes_per_chunk = samples_per_chunk * bytes_per_sample
    
    t_part1_start = time.time()
    print(f"[TIMING] part1_send_start: {t_part1_start:.6f}  pcm1_bytes={len(pcm1)}")
    # Send part 1
    for i in range(0, len(pcm1), bytes_per_chunk):
        chunk_data = pcm1[i:i+bytes_per_chunk]
        if len(chunk_data) < bytes_per_chunk:
            chunk_data = chunk_data.ljust(bytes_per_chunk, b'\0')
        frame = rtc.AudioFrame(data=chunk_data, sample_rate=sample_rate, num_channels=num_channels, samples_per_channel=samples_per_chunk)
        await source.capture_frame(frame)
        await asyncio.sleep(chunk_duration_ms / 1000)
    t_part1_end = time.time()
    print(f"[TIMING] part1_send_end:   {t_part1_end:.6f}  elapsed={t_part1_end-t_part1_start:.3f}s")
        
    t_silence_start = time.time()
    print(f"[TIMING] silence_send_start: {t_silence_start:.6f}  intended_silence=1.200s")
    # Send silence
    for i in range(0, len(silence), bytes_per_chunk):
        chunk_data = silence[i:i+bytes_per_chunk]
        frame = rtc.AudioFrame(data=chunk_data, sample_rate=sample_rate, num_channels=num_channels, samples_per_channel=samples_per_chunk)
        await source.capture_frame(frame)
        await asyncio.sleep(chunk_duration_ms / 1000)
    t_silence_end = time.time()
    print(f"[TIMING] silence_send_end:   {t_silence_end:.6f}  elapsed={t_silence_end-t_silence_start:.3f}s")
        
    t_part2_start = time.time()
    print(f"[TIMING] part2_send_start: {t_part2_start:.6f}  pcm2_bytes={len(pcm2)}")
    # Send part 2
    for i in range(0, len(pcm2), bytes_per_chunk):
        chunk_data = pcm2[i:i+bytes_per_chunk]
        if len(chunk_data) < bytes_per_chunk:
            chunk_data = chunk_data.ljust(bytes_per_chunk, b'\0')
        frame = rtc.AudioFrame(data=chunk_data, sample_rate=sample_rate, num_channels=num_channels, samples_per_channel=samples_per_chunk)
        await source.capture_frame(frame)
        await asyncio.sleep(chunk_duration_ms / 1000)
    t_part2_end = time.time()
    print(f"[TIMING] part2_send_end:   {t_part2_end:.6f}  elapsed={t_part2_end-t_part2_start:.3f}s")
    print(f"[TIMING] total_part1_to_part2_end: {t_part2_end-t_part1_start:.3f}s  (part1={t_part1_end-t_part1_start:.3f}s + silence={t_silence_end-t_silence_start:.3f}s + part2={t_part2_end-t_part2_start:.3f}s)")
        
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
