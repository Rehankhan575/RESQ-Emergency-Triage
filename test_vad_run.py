"""
VAD Isolation Test Runner — Runs 2 & 3
Uses the ALREADY-RUNNING worker (started via run_worker.sh).
Creates a session via FastAPI, connects as caller, injects TTS audio.

Usage:
    python test_vad_run.py 2   # Run 2
    python test_vad_run.py 3   # Run 3

Prerequisites:
    - uvicorn running: uvicorn app.main:app --reload --port 8000
    - worker running:  ./run_worker.sh   (with new VAD logging in voice_agent.py)
"""

import asyncio
import base64
import io
import os
import sys
import time
import wave
import re
import httpx
from dotenv import load_dotenv
from livekit import rtc

load_dotenv()

SARVAM_KEY = os.getenv("SARVAM_API_KEY")

CLAUSE_1 = "Gandhi nagar mein aag lag gayi hai."
CLAUSE_2 = "Aur teen logo ko chot lagi hai."
INTER_CLAUSE_PAUSE_S = 1.0   # silence gap between clauses — the critical VAD test
POST_SPEECH_WAIT_S   = 10.0  # time to wait for VAD+LLM+TTS response after trailing silence


# ─── Sarvam TTS ──────────────────────────────────────────────────────────────

async def synthesize(text: str) -> tuple[bytes, int, int]:
    """Returns (raw_pcm_bytes, sample_rate, num_channels) as 16-bit signed PCM."""
    url = "https://api.sarvam.ai/text-to-speech"
    payload = {
        "inputs": [text],
        "target_language_code": "hi-IN",
        "speaker": "priya",
        "pace": 1.0,
        "speech_sample_rate": 16000,   # 16kHz — valid webrtcvad rate
        "enable_preprocessing": True,
        "model": "bulbul:v3",
    }
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": SARVAM_KEY,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        audio_b64 = resp.json()["audios"][0]

    wav_bytes = base64.b64decode(audio_b64)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        raw_pcm = wf.readframes(wf.getnframes())
        rate    = wf.getframerate()
        ch      = wf.getnchannels()
    return raw_pcm, rate, ch


# ─── Audio publishing helpers ─────────────────────────────────────────────────

def make_silence(duration_s: float, rate: int, channels: int) -> bytes:
    n_samples = int(rate * duration_s)
    return b"\x00" * (n_samples * 2 * channels)


async def publish_pcm(source: rtc.AudioSource, pcm: bytes, rate: int, channels: int,
                      label: str = "") -> float:
    """Push raw PCM into a LiveKit AudioSource in 20ms chunks. Returns end timestamp."""
    frame_ms   = 20
    bps        = 2
    chunk_size = int(rate * frame_ms / 1000) * bps * channels
    t0 = time.time()
    for i in range(0, len(pcm), chunk_size):
        chunk = pcm[i : i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = chunk.ljust(chunk_size, b"\x00")
        n_samples = len(chunk) // (bps * channels)
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=rate,
            num_channels=channels,
            samples_per_channel=n_samples,
        )
        await source.capture_frame(frame)
        await asyncio.sleep(frame_ms / 1000.0)
    elapsed = time.time() - t0
    end_ts = time.time()
    if label:
        print(f"  [inject] {label}: {len(pcm)} bytes, {elapsed:.2f}s, ended_at={end_ts:.3f}",
              flush=True)
    return end_ts


# ─── Main test coroutine ──────────────────────────────────────────────────────

async def run_test(run_id: int):
    print(f"\n{'='*60}", flush=True)
    print(f"VAD ISOLATION TEST — RUN {run_id}", flush=True)
    print(f"Using already-running worker (no subprocess spawn)", flush=True)
    print(f"{'='*60}", flush=True)

    # 1. Synthesize both clauses
    print("Synthesizing clause 1 via Sarvam TTS...", flush=True)
    pcm1, rate1, ch1 = await synthesize(CLAUSE_1)
    dur1 = len(pcm1) / (rate1 * 2 * ch1)
    print(f"  clause1: {len(pcm1)} bytes @ {rate1}Hz ch={ch1}, duration={dur1:.2f}s", flush=True)

    print("Synthesizing clause 2 via Sarvam TTS...", flush=True)
    pcm2, rate2, ch2 = await synthesize(CLAUSE_2)
    dur2 = len(pcm2) / (rate2 * 2 * ch2)
    print(f"  clause2: {len(pcm2)} bytes @ {rate2}Hz ch={ch2}, duration={dur2:.2f}s", flush=True)

    silence  = make_silence(INTER_CLAUSE_PAUSE_S, rate1, ch1)
    trailing = make_silence(0.8, rate1, ch1)   # 800ms trailing silence > 500ms VAD threshold
    print(f"  inter-clause silence: {INTER_CLAUSE_PAUSE_S}s", flush=True)
    print(f"  trailing silence: 0.8s (> VAD_SILENCE_MS=500ms)", flush=True)

    # 2. Create session via FastAPI (registers it in DB), then mint our own caller token
    print("\nCreating call session via FastAPI /api/calls/start...", flush=True)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post("http://localhost:8000/api/calls/start")
        resp.raise_for_status()
        session    = resp.json()
        session_id = session["session_id"]
    print(f"  session_id={session_id}", flush=True)

    # Mint a caller token directly — same logic as worker.py /token endpoint
    import uuid as _uuid
    from livekit import api as lkapi
    lk_url    = os.getenv("LIVEKIT_URL")
    lk_key    = os.getenv("LIVEKIT_API_KEY")
    lk_secret = os.getenv("LIVEKIT_API_SECRET")
    caller_id = f"caller-{_uuid.uuid4().hex[:8]}"
    tok = lkapi.AccessToken(lk_key, lk_secret) \
        .with_identity(caller_id) \
        .with_name("VAD Test Caller") \
        .with_grants(lkapi.VideoGrants(room_join=True, room=session_id))
    lk_token  = tok.to_jwt()
    print(f"  lk_url={lk_url}", flush=True)
    print(f"  caller_id={caller_id}", flush=True)

    # 3. Connect to LiveKit room as caller
    print("\nConnecting to LiveKit room...", flush=True)
    room = rtc.Room()
    await room.connect(lk_url, lk_token)
    print(f"  Connected. Room: {room.name}", flush=True)

    # 4. Publish audio track
    source = rtc.AudioSource(rate1, ch1)
    track  = rtc.LocalAudioTrack.create_audio_track("caller-mic", source)
    opts   = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, opts)
    print(f"  Audio track published", flush=True)

    # 5. Wait for worker to join room and play greeting (~6s)
    print("\nWaiting 7s for agent to join room and play greeting...", flush=True)
    await asyncio.sleep(7.0)

    # 6. Inject clause 1
    t_c1_start = time.time()
    print(f"\n[{t_c1_start:.3f}] Injecting CLAUSE 1: '{CLAUSE_1}'", flush=True)
    t_c1_end = await publish_pcm(source, pcm1, rate1, ch1, label="clause1")

    # 7. Inject inter-clause silence (the 1s pause the user makes between clauses)
    print(f"[{t_c1_end:.3f}] Injecting {INTER_CLAUSE_PAUSE_S}s inter-clause silence...", flush=True)
    t_sil_end = await publish_pcm(source, silence, rate1, ch1, label="inter_clause_silence")

    # 8. Inject clause 2
    print(f"[{t_sil_end:.3f}] Injecting CLAUSE 2: '{CLAUSE_2}'", flush=True)
    t_c2_end = await publish_pcm(source, pcm2, rate2, ch2, label="clause2")

    # 9. Trailing silence to cross the VAD 500ms threshold
    print(f"[{t_c2_end:.3f}] Injecting 0.8s trailing silence to trigger VAD...", flush=True)
    t_trail_end = await publish_pcm(source, trailing, rate1, ch1, label="trailing_silence")

    # This is the TRUE silence start — VAD should fire within ~0ms of this (threshold already crossed)
    t_true_silence_start = t_c2_end

    print(f"\n[{t_trail_end:.3f}] All audio injected.", flush=True)
    print(f"  TRUE silence start (clause2 end): {t_true_silence_start:.3f}", flush=True)
    print(f"  Trailing silence end:              {t_trail_end:.3f}", flush=True)
    print(f"Waiting {POST_SPEECH_WAIT_S}s for VAD+LLM+TTS to complete...", flush=True)
    await asyncio.sleep(POST_SPEECH_WAIT_S)

    # 10. Disconnect
    print("\nDisconnecting from room...", flush=True)
    await room.disconnect()
    await asyncio.sleep(1.0)

    # 11. Print timing summary
    print(f"\n{'─'*60}", flush=True)
    print(f"INJECTION TIMING — RUN {run_id}", flush=True)
    print(f"{'─'*60}", flush=True)
    print(f"  Clause 1 start:           {t_c1_start:.3f}", flush=True)
    print(f"  Clause 1 end:             {t_c1_end:.3f}  ({t_c1_end-t_c1_start:.3f}s)", flush=True)
    print(f"  Inter-clause silence end: {t_sil_end:.3f}  (+{INTER_CLAUSE_PAUSE_S}s pause)", flush=True)
    print(f"  Clause 2 end (TRUE silence start): {t_c2_end:.3f}", flush=True)
    print(f"  Trailing silence end:     {t_trail_end:.3f}  (+0.8s)", flush=True)
    print(f"\n  >>> Check worker terminal/logs for: vad_turn_boundary, transcript_finalized finalized_by=", flush=True)
    print(f"  >>> VAD should fire within ~500ms of {t_c2_end:.3f}", flush=True)
    print(f"{'='*60}\n", flush=True)


async def main():
    run_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    await run_test(run_id)


if __name__ == "__main__":
    asyncio.run(main())
