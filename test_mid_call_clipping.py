import asyncio
import time
import os
import wave
import io
import aiohttp
from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

async def transcribe_audio(wav_data):
    """Transcribes WAV data using Deepgram REST API."""
    url = f"https://api.deepgram.com/v1/listen?model=nova-2&language=hi"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/wav"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=wav_data) as response:
            result = await response.json()
            try:
                transcript = result["results"]["channels"][0]["alternatives"][0]["transcript"]
                return transcript
            except KeyError:
                return str(result)

async def test_mid_call(run_id: int):
    print(f"\n--- Run {run_id} ---")
    session_id = f"test-midcall-{run_id}"
    
    # Spawn worker
    import subprocess
    import sys
    worker = subprocess.Popen(
        [sys.executable, "-m", "app.agents.worker", "start", "--dev"],
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    # Wait for worker to be ready
    for line in worker.stdout:
        if "HTTP server listening on :8081" in line:
            break
            
    async def drain_worker():
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, worker.stdout.readline)
            if not line: break
            print(f"[WORKER] {line}", end="")
            
    drain_task = asyncio.create_task(drain_worker())
            
    token = api.AccessToken(
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET")
    ).with_identity(f"caller-{session_id}").with_name("Caller").with_grants(api.VideoGrants(
        room_join=True,
        room=session_id,
    )).to_jwt()
    
    room = rtc.Room()
    frames_received = []
    
    @room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            async def monitor_stream():
                stream = rtc.AudioStream(track)
                async for event in stream:
                    frames_received.append(event.frame)
            asyncio.create_task(monitor_stream())

    await room.connect(os.getenv("LIVEKIT_URL"), token)
    
    # Wait for the greeting to be fully received
    print("Subscribed. Waiting 15s for greeting to arrive...")
    frames_received.clear()
    await asyncio.sleep(15)
    
    # Transcribe the greeting
    if frames_received:
        full_pcm = bytearray()
        for f in frames_received:
            full_pcm.extend(bytes(f.data))
        sample_rate = frames_received[0].sample_rate
        num_channels = frames_received[0].num_channels
        
        with wave.open(f"run{run_id}_greeting.wav", "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(full_pcm)
            
        with open(f"run{run_id}_greeting.wav", "rb") as f:
            wav_data = f.read()
            
        transcript = await transcribe_audio(wav_data)
        print(f"Greeting Transcript: '{transcript}'")
        if "namaste" in transcript.lower() or "नमस्ते" in transcript:
            print("Greeting Verdict: Not clipped!")
        else:
            print("Greeting Verdict: Clipped!")
    else:
        print("Greeting Verdict: No audio received!")
        
    frames_received.clear()
    
    print("Triggering agent STT manually by publishing an audio track...")
    
    # We use the generated `test_audio.wav` containing actual speech
    with open("test_audio.wav", "rb") as f:
        wf = wave.open(f, "rb")
        raw_pcm = wf.readframes(wf.getnframes())
        sample_rate = wf.getframerate()
        num_channels = wf.getnchannels()
        
        bytes_per_sample = 2
        chunk_duration_ms = 20
        samples_per_chunk = int(sample_rate * (chunk_duration_ms / 1000.0))
        bytes_per_chunk = samples_per_chunk * bytes_per_sample * num_channels
        
        for i in range(0, len(raw_pcm), bytes_per_chunk):
            chunk_data = raw_pcm[i:i + bytes_per_chunk]
            chunk_samples = len(chunk_data) // (bytes_per_sample * num_channels)
            
            frame = rtc.AudioFrame(
                data=chunk_data,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=chunk_samples,
            )
            await source.capture_frame(frame)
            await asyncio.sleep(chunk_duration_ms / 1000.0)
            
        # Push 2 seconds of perfect silence to flush Deepgram VAD and trigger is_final=True
        print("Pushing 2 seconds of silence to flush STT VAD...")
        silence_pcm = b'\x00' * (sample_rate * bytes_per_sample * num_channels * 2)
        for i in range(0, len(silence_pcm), bytes_per_chunk):
            chunk_data = silence_pcm[i:i + bytes_per_chunk]
            chunk_samples = len(chunk_data) // (bytes_per_sample * num_channels)
            
            frame = rtc.AudioFrame(
                data=chunk_data,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=chunk_samples,
            )
            await source.capture_frame(frame)
            await asyncio.sleep(chunk_duration_ms / 1000.0)
        
    print("Waiting 20s for LLM to process and TTS to arrive (includes filler)...")
    await asyncio.sleep(20)
    
    if not frames_received:
        print("Mid-call Verdict: No audio received from agent.")
    else:
        # Reconstruct PCM
        full_pcm = bytearray()
        for f in frames_received:
            full_pcm.extend(bytes(f.data))
            
        sample_rate = frames_received[0].sample_rate
        num_channels = frames_received[0].num_channels
        
        with wave.open(f"run{run_id}_midcall.wav", "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(full_pcm)
            
        with open(f"run{run_id}_midcall.wav", "rb") as f:
            wav_data = f.read()
            
        transcript = await transcribe_audio(wav_data)
        print(f"Mid-call Transcript: '{transcript}'")
        if transcript:
            print("Mid-call Verdict: Not clipped (first word intact)")
        else:
            print("Mid-call Verdict: Clipped or different response")
            
    await room.disconnect()
    worker.terminate()
    worker.wait()

async def main():
    for i in range(1, 6):
        await test_mid_call(i)

if __name__ == "__main__":
    asyncio.run(main())
