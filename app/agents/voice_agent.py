import os
import time
import asyncio
import logging
import array
import httpx
import base64
import wave
import io
import random
from typing import AsyncIterator, Optional
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.plugins import deepgram

from app.agents.triage.agent import process_turn

load_dotenv()
logger = logging.getLogger("voice_agent")
logger.setLevel(logging.INFO)

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Kill-switches: set either env var to "false" to restore previous behavior.
VAD_SUPPORTED_RATES = {8000, 16000, 32000, 48000}
VAD_FRAME_MS = 20
VAD_SILENCE_MS = int(os.getenv("VAD_SILENCE_MS", "800"))
VAD_AGGRESSIVENESS = 3

# ── Persistent HTTP client (Fix 4) ──────────────────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


# ── Environment flags ────────────────────────────────────────────────────────
ENABLE_FILLERS = os.getenv("ENABLE_FILLERS", "false").lower() in ("true", "1", "yes")

# ── Greeting TTS cache (synthesize once to disk, reuse across process launches) ─
_GREETING_TEXT = "Namaste, main aapki madad ke liye yahan hoon. Kripya apni emergency bataayein."
GREETING_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "greeting_cache.wav"
)
_greeting_cache: Optional[bytes] = None
_greeting_cache_lock: Optional[asyncio.Lock] = None


def _get_greeting_lock() -> asyncio.Lock:
    """Lazily create the lock inside the running event loop."""
    global _greeting_cache_lock
    if _greeting_cache_lock is None:
        _greeting_cache_lock = asyncio.Lock()
    return _greeting_cache_lock


async def _get_greeting_pcm() -> bytes:
    """
    Return the greeting WAV bytes.
    1. If in-memory cache exists, return it immediately.
    2. If disk cache (app/assets/greeting_cache.wav) exists, load from disk.
    3. If neither exists, synthesize via Sarvam TTS once, save to disk, and return.
    """
    global _greeting_cache
    if _greeting_cache is not None:
        logger.info("greeting_cache_hit (memory): reusing pre-synthesized greeting bytes")
        return _greeting_cache

    if os.path.isfile(GREETING_CACHE_FILE):
        try:
            with open(GREETING_CACHE_FILE, "rb") as f:
                _greeting_cache = f.read()
            if _greeting_cache:
                logger.info(
                    f"greeting_cache_hit (disk): loaded {len(_greeting_cache)} bytes from {GREETING_CACHE_FILE}"
                )
                return _greeting_cache
        except Exception as e:
            logger.warning(f"Failed to read greeting cache file {GREETING_CACHE_FILE}: {e}")

    async with _get_greeting_lock():
        if _greeting_cache is not None:
            return _greeting_cache
        if os.path.isfile(GREETING_CACHE_FILE):
            try:
                with open(GREETING_CACHE_FILE, "rb") as f:
                    _greeting_cache = f.read()
                if _greeting_cache:
                    return _greeting_cache
            except Exception:
                pass

        logger.info("greeting_cache_miss: synthesizing greeting via Sarvam TTS")
        _greeting_cache = await synthesize_tts(_GREETING_TEXT, voice="priya")
        logger.info(
            f"greeting_cache_filled: {len(_greeting_cache)} bytes, saving to disk: {GREETING_CACHE_FILE}"
        )
        try:
            os.makedirs(os.path.dirname(GREETING_CACHE_FILE), exist_ok=True)
            with open(GREETING_CACHE_FILE, "wb") as f:
                f.write(_greeting_cache)
        except Exception as e:
            logger.warning(f"Failed to write greeting cache file {GREETING_CACHE_FILE}: {e}")

        return _greeting_cache


def _pcm16_to_mono(pcm: bytes, num_channels: int) -> bytes:
    if num_channels <= 1:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % (2 * num_channels))])
    mono = array.array("h", (samples[i] for i in range(0, len(samples), num_channels)))
    return mono.tobytes()


def _resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n_src = len(src)
    n_dst = max(1, int(n_src * dst_rate / src_rate))
    dst = array.array("h", [0] * n_dst)
    for i in range(n_dst):
        x = i * (n_src - 1) / max(n_dst - 1, 1)
        j = int(x)
        frac = x - j
        if j + 1 < n_src:
            dst[i] = int(src[j] * (1 - frac) + src[j + 1] * frac)
        else:
            dst[i] = src[j]
    return dst.tobytes()


class VadEndpointer:
    """Client-side WebRTC VAD: 500ms silence after speech => end of turn."""

    def __init__(self):
        import webrtcvad

        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._buf = bytearray()
        self._rate: Optional[int] = None
        self._frame_bytes: int = 0
        self.in_speech = False
        self._speech_seen = False
        self._silence_ms = 0

    def feed(self, frame: rtc.AudioFrame) -> bool:
        """Return True when ~500ms of continuous silence follows detected speech."""
        pcm = bytes(frame.data)
        rate = int(frame.sample_rate)
        pcm = _pcm16_to_mono(pcm, int(frame.num_channels))
        if rate not in VAD_SUPPORTED_RATES:
            pcm = _resample_pcm16(pcm, rate, 16000)
            rate = 16000

        if self._rate is None:
            self._rate = rate
            self._frame_bytes = int(rate * VAD_FRAME_MS / 1000) * 2
        elif rate != self._rate:
            pcm = _resample_pcm16(pcm, rate, self._rate)

        self._buf.extend(pcm)
        ended = False
        while self._frame_bytes and len(self._buf) >= self._frame_bytes:
            chunk = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            try:
                is_speech = self._vad.is_speech(chunk, self._rate)
            except Exception as exc:
                logger.warning(f"VAD frame skipped: {exc}")
                continue
            if is_speech:
                self.in_speech = True
                self._speech_seen = True
                self._silence_ms = 0
            else:
                self.in_speech = False
                if self._speech_seen:
                    self._silence_ms += VAD_FRAME_MS
                    if self._silence_ms >= VAD_SILENCE_MS:
                        ended = True
                        self._speech_seen = False
                        self._silence_ms = 0
        return ended


def _try_strip_wav_header(buf: bytearray) -> tuple[bytearray, Optional[int], Optional[int], bool]:
    """
    If `buf` starts with a WAV header, wait until the `data` chunk is found and
    strip the header. Returns (remaining_bytes, sample_rate, channels, ready).
    `ready` is False while still waiting for a complete WAV header.
    """
    if len(buf) < 12:
        return buf, None, None, False
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return buf, None, None, True

    offset = 12
    sample_rate = None
    num_channels = None
    while offset + 8 <= len(buf):
        chunk_id = bytes(buf[offset : offset + 4])
        chunk_size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        if chunk_id == b"fmt " and data_start + 16 <= len(buf):
            num_channels = int.from_bytes(buf[data_start + 2 : data_start + 4], "little")
            sample_rate = int.from_bytes(buf[data_start + 4 : data_start + 8], "little")
        if chunk_id == b"data":
            return bytearray(buf[data_start:]), sample_rate, num_channels, True
        if data_start + chunk_size > len(buf):
            return buf, sample_rate, num_channels, False
        offset = data_start + chunk_size
        if chunk_size % 2:
            offset += 1
    return buf, sample_rate, num_channels, False


async def broadcast_to_backend(session_id: str, event_type: str, data: dict) -> None:
    """Fire-and-forget broadcast from worker process → FastAPI → WebSocket clients."""
    url = f"http://localhost:8000/api/calls/{session_id}/broadcast"
    payload = {"event_type": event_type, "data": data}
    try:
        client = await _get_http_client()
        await client.post(url, json=payload, timeout=2.0)
    except Exception as e:
        logger.warning(f"Failed to broadcast to backend: {e}")


# ── TTS via Sarvam ───────────────────────────────────────────────────────────
async def synthesize_tts(text: str, voice: str = "priya") -> bytes:
    """Calls Sarvam TTS API via HTTP."""
    if not SARVAM_API_KEY:
        raise ValueError("SARVAM_API_KEY is not set.")

    url = "https://api.sarvam.ai/text-to-speech"
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": SARVAM_API_KEY,
    }
    payload = {
        "inputs": [text],
        "target_language_code": "hi-IN",
        "speaker": voice,
        "pace": 1.0,
        "speech_sample_rate": 8000,
        "enable_preprocessing": True,
        "model": "bulbul:v3",
    }

    client = await _get_http_client()
    response = await client.post(url, json=payload, headers=headers, timeout=10.0)
    response.raise_for_status()
    audio_b64 = response.json().get("audios", [""])[0]
    return base64.b64decode(audio_b64)


async def synthesize_tts_streaming(
    text: str, voice: str = "priya"
) -> AsyncIterator[bytes]:
    """
    POST /text-to-speech/stream and yield binary audio as chunks arrive.
    Same auth/voice/model as synthesize_tts(); does not replace it.
    """
    if not SARVAM_API_KEY:
        raise ValueError("SARVAM_API_KEY is not set.")

    url = "https://api.sarvam.ai/text-to-speech/stream"
    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": SARVAM_API_KEY,
    }
    payload = {
        "text": text,
        "target_language_code": "hi-IN",
        "speaker": voice,
        "pace": 1.0,
        "speech_sample_rate": 8000,
        "enable_preprocessing": True,
        "model": "bulbul:v3",
        "output_audio_codec": "linear16",
    }

    client = await _get_http_client()
    timeout = httpx.Timeout(30.0, connect=10.0)
    received = 0
    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=timeout
        ) as response:
            if response.status_code != 200:
                body = (await response.aread())[:500]
                logger.error(
                    f"TTS stream HTTP {response.status_code}: {body!r}"
                )
                response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if chunk:
                    received += len(chunk)
                    yield chunk
    except Exception as exc:
        if received == 0:
            raise
        logger.warning(
            f"TTS stream interrupted after {received} bytes: {exc}; "
            "using partial audio already received"
        )


# ── Default cumulative state template ────────────────────────────────────────
def _default_state() -> dict:
    return {
        "location": None,
        "emergency_type": None,
        "people_affected": None,
        "injuries": None,
        "severity": None,
        "confidence": 0.0,
        "flag_for_human": False,
        "next_question": "",
        "caller_stress_level": "calm",
        "is_prank": False,
    }


class TriageVoiceAgent:
    def __init__(self, ctx: agents.JobContext, session_id: str = "unknown"):
        self.ctx = ctx
        self.session_id = session_id

        # Filler logic
        self.playback_lock = asyncio.Lock()
        self.last_filler_index: Optional[int] = None
        self.filler_played_this_turn: bool = False
        # Part B: per-turn filler gate keyed by turn_id so it survives model fallback retries
        self._filler_played_for_turn_id: Optional[str] = None
        self.filler_pcms: list[tuple[bytes, int, int]] = []
        if ENABLE_FILLERS:
            filler_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "fillers")
            try:
                if not os.path.exists(filler_dir):
                    logger.warning(f"filler_load_failed: Directory {filler_dir} not found, continuing without filler playback")
                else:
                    for f in sorted(os.listdir(filler_dir)):
                        if f.endswith(".wav"):
                            with wave.open(os.path.join(filler_dir, f), "rb") as wf:
                                raw_pcm = wf.readframes(wf.getnframes())
                                sample_rate = wf.getframerate()
                                num_channels = wf.getnchannels()
                                self.filler_pcms.append((raw_pcm, sample_rate, num_channels))
                    if not self.filler_pcms:
                        logger.warning("filler_load_failed: No valid wav files found, continuing without filler playback")
                    else:
                        logger.info(f"Loaded {len(self.filler_pcms)} filler clips")
            except Exception as e:
                logger.warning(f"filler_load_failed: {e}, continuing without filler playback")
                self.filler_pcms = []
        else:
            logger.info("Fillers disabled (ENABLE_FILLERS=false)")

        # Conversation history — alternating user / model strings
        self.history: list[str] = []

        # Fix 2: cumulative triage state, failure counter, one-way escalation flag
        self.current_state: dict = _default_state()
        self.consecutive_failures: int = 0
        self.escalated: bool = False

        # Fix 5: call start timestamp (forward-compatible, not yet broadcast)
        self.started_at: float = time.time()

        # Deepgram STT
        self.stt = deepgram.STT(
            api_key=DEEPGRAM_API_KEY,
            language="hi",
            model="nova-2",
            endpointing_ms=1000,
        )

        # Audio source and track for sending TTS audio back to the room
        self.audio_source = rtc.AudioSource(8000, 1)
        self.audio_track = rtc.LocalAudioTrack.create_audio_track(
            "agent-mic", self.audio_source
        )

        self.audio_stream_tasks: list[asyncio.Task] = []

        # Fix 3: turn queue — decouples STT from LLM/TTS processing
        self.turn_queue: asyncio.Queue = asyncio.Queue()

        # Playback gate: timestamp when agent audio last finished; used to suppress
        # STT/VAD feeding while the agent is speaking and for a brief grace window.
        self._agent_playback_ended_at: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info(
            f"[CONFIG] session={self.session_id} "
            f"USE_VAD_TURN_DETECTION={os.getenv('USE_VAD_TURN_DETECTION', 'true')} "
            f"USE_STREAMING_TTS={os.getenv('USE_STREAMING_TTS', 'true')} "
            f"ENABLE_FILLERS={ENABLE_FILLERS}"
        )
        # Greet caller — synthesize concurrently with track publishing so bytes
        # are ready (or already cached) by the time the track is subscribed.
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)

        async def _publish_and_subscribe():
            publication = await self.ctx.room.local_participant.publish_track(self.audio_track, options)
            await publication.wait_for_subscription()
            logger.info("Agent audio track published successfully.")

        greeting_pcm, _ = await asyncio.gather(
            _get_greeting_pcm(),
            _publish_and_subscribe(),
        )


        # Fix 3: start the background turn worker
        asyncio.create_task(self._turn_worker())

        @self.ctx.room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant):
            logger.info(f"Participant connected: {participant.identity}")

        @self.ctx.room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                logger.info(f"Subscribed to audio track from {participant.identity}")
                task = asyncio.create_task(self.process_audio_track(track))
                self.audio_stream_tasks.append(task)

        # Handle participants who joined before the agent
        for participant in self.ctx.room.remote_participants.values():
            logger.info(f"Found existing participant: {participant.identity}")
            for publication in participant.track_publications.values():
                if publication.track and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
                    logger.info(
                        f"Found existing audio track from {participant.identity}, subscribing..."
                    )
                    task = asyncio.create_task(
                        self.process_audio_track(publication.track)
                    )
                    self.audio_stream_tasks.append(task)

        # Broadcast greeting text and play the pre-synthesized PCM bytes
        await broadcast_to_backend(
            self.session_id,
            "transcript_chunk",
            {"speaker": "agent", "text": _GREETING_TEXT},
        )
        logger.info("TTS synthesis completed. Publishing audio to room...")
        await self.play_audio(greeting_pcm)

    # ── STT pipeline ─────────────────────────────────────────────────────────

    async def process_audio_track(self, track: rtc.RemoteAudioTrack):
        logger.info("STT stream processing started for user audio track.")
        audio_stream = rtc.AudioStream(track)
        stt_stream = self.stt.stream()

        pending_transcript_buffer = []
        pending_debounce_task: asyncio.Task | None = None
        last_speech_end_time = 0.0
        last_receipt_time = 0.0
        use_vad = os.getenv("USE_VAD_TURN_DETECTION", "true") == "true"
        vad_endpointer: Optional[VadEndpointer] = None
        if use_vad:
            try:
                vad_endpointer = VadEndpointer()
            except Exception as exc:
                logger.warning(f"VAD init failed ({exc}); falling back to 2000ms debounce")
                use_vad = False
        vad_end_event = asyncio.Event()
        vad_waiting_for_final = False

        logger.info(
            f"Turn detection: {'client VAD (~500ms silence)' if use_vad else 'Deepgram FINAL + 2000ms debounce'}"
        )

        async def trigger_turn(finalized_by: str = "unknown"):
            if not pending_transcript_buffer:
                return
            full_transcript = " ".join(pending_transcript_buffer)
            pending_transcript_buffer.clear()
            logger.info(f"transcript_finalized finalized_by={finalized_by}")
            await self.turn_queue.put(full_transcript)

        async def push_frames():
            nonlocal vad_waiting_for_final
            _was_gated = False
            async for frame_event in audio_stream:
                # ── Playback gate: suppress caller audio while agent is speaking
                # and for a 300ms echo/buffer grace window after playback ends. ──
                _now = time.time()
                _gated = self.playback_lock.locked() or (_now - self._agent_playback_ended_at) < 0.3
                if _gated:
                    _was_gated = True
                    continue

                # First frame after gate closes: reset VAD state to prevent
                # stale partial-speech data from firing a false boundary.
                if _was_gated:
                    _was_gated = False
                    if use_vad and vad_endpointer is not None:
                        vad_endpointer._speech_seen = False
                        vad_endpointer._silence_ms = 0

                stt_stream.push_frame(frame_event.frame)
                if use_vad and vad_endpointer is not None:
                    ended = vad_endpointer.feed(frame_event.frame)
                    if vad_endpointer.in_speech:
                        vad_waiting_for_final = False
                    if ended:
                        logger.info(f"vad_turn_boundary: VAD_SILENCE_MS={VAD_SILENCE_MS}ms threshold crossed — firing end event")
                        vad_end_event.set()

        async def read_events():
            nonlocal pending_debounce_task, last_speech_end_time, last_receipt_time
            nonlocal vad_waiting_for_final

            async def debounce_waiter():
                await asyncio.sleep(2.0)
                if pending_transcript_buffer:
                    logger.info("debounce_path: 2000ms debounce expired; finalizing")
                    await trigger_turn(finalized_by="debounce")

            async for event in stt_stream:
                if event.type == agents.stt.SpeechEventType.FINAL_TRANSCRIPT:
                    transcript = event.alternatives[0].text
                    if transcript.strip():
                        current_time = time.time()
                        receipt_gap = current_time - last_receipt_time if last_receipt_time > 0 else 0
                        last_receipt_time = current_time
                        
                        speech_start = getattr(event, 'speech_start_time', 0.0)
                        if speech_start is None: speech_start = 0.0
                        speech_end = getattr(event, 'speech_end_time', 0.0)
                        if speech_end is None: speech_end = 0.0
                        
                        speech_gap = speech_start - last_speech_end_time if last_speech_end_time > 0 else 0
                        last_speech_end_time = speech_end
                        
                        logger.info(f"STT Transcript Received: '{transcript}' | receipt_gap={receipt_gap:.3f}s | speech_start={speech_start:.3f}s | speech_end={speech_end:.3f}s | speech_gap={speech_gap:.3f}s")
                        
                        # Reset filler flag at the start of each new turn
                        if not pending_transcript_buffer:
                            self.filler_played_this_turn = False


                        pending_transcript_buffer.append(transcript.strip())

                        # Broadcast user transcript immediately — don't wait for LLM
                        await broadcast_to_backend(
                            self.session_id,
                            "transcript_chunk",
                            {"speaker": "user", "text": transcript},
                        )

                        if use_vad:
                            if vad_waiting_for_final:
                                vad_waiting_for_final = False
                                logger.info("vad_path=A: VAD fired before FINAL; finalizing now that FINAL arrived")
                                await trigger_turn(finalized_by="VAD-pathA")
                        else:
                            if pending_debounce_task and not pending_debounce_task.done():
                                pending_debounce_task.cancel()
                            pending_debounce_task = asyncio.create_task(debounce_waiter())

        async def vad_finalize_loop():
            nonlocal vad_waiting_for_final
            while True:
                await vad_end_event.wait()
                vad_end_event.clear()
                if pending_transcript_buffer:
                    logger.info("vad_path=B: VAD fired after FINAL already buffered; finalizing now")
                    await trigger_turn(finalized_by="VAD-pathB")
                else:
                    vad_waiting_for_final = True
                    logger.info("vad_path=C: VAD fired but no FINAL yet; setting vad_waiting_for_final=True")

        try:
            if use_vad:
                vad_task = asyncio.create_task(vad_finalize_loop())
                try:
                    await asyncio.gather(push_frames(), read_events())
                finally:
                    vad_task.cancel()
            else:
                await asyncio.gather(push_frames(), read_events())
        except Exception as e:
            logger.error(f"Error in audio processing pipeline: {e}")

    # ── Fix 3: background turn worker ────────────────────────────────────────

    async def _turn_worker(self):
        """Drains turn_queue sequentially so STT is never blocked by LLM/TTS."""
        turn_seq = 0
        while True:
            transcript = await self.turn_queue.get()
            turn_seq += 1
            # Part B: generate a turn_id and reset the per-turn filler gate
            current_turn_id = f"turn-{turn_seq}"
            self._filler_played_for_turn_id = None
            logger.info(f"LLM Turn Handler Invocation #{turn_seq}: Sending transcript exactly as: '{transcript}'")
            try:
                await self.on_user_speech(transcript, turn_id=current_turn_id)
            except Exception as e:
                logger.error(f"Error processing queued turn: {e}")
            finally:
                self.turn_queue.task_done()

    # ── Core turn handler ─────────────────────────────────────────────────────

    async def on_user_speech(self, transcript: str, turn_id: str = "unknown"):
        # Part C: bail immediately if we've already escalated — no more AI turns
        if self.escalated:
            return

        logger.info(f"Processing LLM Turn for: '{transcript}'")

        # Fix 1: snapshot history BEFORE this turn, then append transcript
        history_before_turn = list(self.history)
        self.history.append(transcript)

        # Single task: if fillers enabled, wait 1.3s before playing a filler clip.
        # If fillers disabled, wait directly for LLM response without any filler interruption.
        task = asyncio.create_task(
            process_turn(transcript, history_before_turn, previous_state=self.current_state)
        )
        success, result = False, {}
        try:
            if ENABLE_FILLERS and self.filler_pcms:
                # Fast path: wait 1.3s — most turns with a warm model finish here
                done, _ = await asyncio.wait({task}, timeout=1.3)
                if task in done:
                    success, result = task.result()
                else:
                    # LLM is slow — play one filler clip so caller doesn't hear dead air.
                    if self._filler_played_for_turn_id != turn_id:
                        self._filler_played_for_turn_id = turn_id
                        self.filler_played_this_turn = True
                        available = [i for i in range(len(self.filler_pcms)) if i != self.last_filler_index]
                        if not available:
                            available = [self.last_filler_index] if self.last_filler_index is not None else [0]
                        self.last_filler_index = random.choice(available)
                        raw_pcm, sample_rate, num_channels = self.filler_pcms[self.last_filler_index]
                        logger.info("filler_playback_start (adaptive: LLM exceeded 1.3s)")
                        await self.play_audio_pcm(raw_pcm, sample_rate, num_channels, is_real=False)
                    # Wait for the real result with the outer deadline
                    success, result = await asyncio.wait_for(task, timeout=12.0)
            else:
                # Fillers disabled: wait directly for LLM turn without filler
                success, result = await asyncio.wait_for(task, timeout=12.0)
        except asyncio.TimeoutError:
            task.cancel()
            logger.warning("LLM call exceeded combined 12s timeout; treating as soft failure.")
        except Exception as e:
            logger.error(f"Unexpected error awaiting process_turn: {e}")

        if success:
            # Fix 2: reset failure counter
            self.consecutive_failures = 0

            # Fix 2: merge — never let None overwrite a previously known value
            for key, value in result.items():
                if value is not None:
                    self.current_state[key] = value

            logger.info(f"Triage State (merged): {self.current_state}")
            await broadcast_to_backend(
                self.session_id, "triage_update", self.current_state
            )

            if self.current_state.get("flag_for_human"):
                await self._trigger_escalation()
            else:
                next_q = self.current_state.get("next_question", "")
                if next_q:
                    self.history.append(next_q)
                    await self.say(next_q)
        else:
            # Fix 2: failure path — do NOT touch current_state
            self.consecutive_failures += 1
            logger.warning(
                f"LLM call failed (consecutive_failures={self.consecutive_failures})"
            )
            if self.consecutive_failures >= 2:
                await self._trigger_escalation()
            else:
                retry_line = (
                    "Maaf kijiye, main samajh nahi paaya. Kripya dobara bataayein."
                )
                self.history.append(retry_line)
                await self.say(retry_line)

    # ── Fix 2: one-way escalation helper ─────────────────────────────────────

    async def _trigger_escalation(self):
        if self.escalated:
            return  # Guard: never trigger twice
        self.escalated = True
        # Part C: drain any queued turns so the worker doesn't process them after escalation
        while not self.turn_queue.empty():
            try:
                self.turn_queue.get_nowait()
                self.turn_queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.current_state["flag_for_human"] = True
        await broadcast_to_backend(
            self.session_id, "triage_update", self.current_state
        )
        escalation_msg = "Aapki situation mein human assistance zaroori hai. Main abhi ek officer se connect kar raha hoon — please line par rahein."
        self.history.append(escalation_msg)
        logger.info("ESCALATION TRIGGERED: Handing off to human operator.")
        await self.say(escalation_msg)

    # ── TTS + audio playback ──────────────────────────────────────────────────

    async def say(self, text: str):
        """Synthesize text to speech, broadcast to dashboard, and play to room."""
        logger.info(f"TTS synthesis started for text: '{text}'")
        # Broadcast agent text immediately so dashboard updates before audio plays
        await broadcast_to_backend(
            self.session_id,
            "transcript_chunk",
            {"speaker": "agent", "text": text},
        )
        try:
            if os.getenv("USE_STREAMING_TTS", "true") == "true":
                logger.info("TTS streaming path enabled")
                await self.play_audio_stream(synthesize_tts_streaming(text, voice="priya"))
            else:
                audio_data = await synthesize_tts(text, voice="priya")
                logger.info("TTS synthesis completed. Publishing audio to room...")
                await self.play_audio(audio_data)
        except Exception as e:
            logger.error(f"TTS API Error: {e}")

    async def _push_pcm_frame(
        self, chunk_data: bytes, sample_rate: int, num_channels: int, bytes_per_sample: int = 2
    ) -> None:
        chunk_samples = len(chunk_data) // (bytes_per_sample * num_channels)
        if chunk_samples <= 0:
            return
        frame = rtc.AudioFrame(
            data=chunk_data,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=chunk_samples,
        )
        await self.audio_source.capture_frame(frame)

    async def play_audio_stream(self, chunk_iter: AsyncIterator[bytes]):
        """Play Sarvam HTTP stream as 20ms PCM frames as chunks arrive."""
        logger.info("real_response_ready")
        async with self.playback_lock:
            ts = time.time()
            logger.info(f"[{ts:.3f}] real_response_playback_start (streaming 20ms chunks)")
            leftover = bytearray()
            header_ready = False
            sample_rate = 8000
            num_channels = 1
            bytes_per_sample = 2
            chunk_duration_ms = 20
            bytes_per_chunk = int(sample_rate * (chunk_duration_ms / 1000.0)) * bytes_per_sample * num_channels
            received = 0
            try:
                async for chunk in chunk_iter:
                    leftover.extend(chunk)
                    received += len(chunk)
                    if not header_ready:
                        leftover, parsed_rate, parsed_ch, header_ready = _try_strip_wav_header(leftover)
                        if header_ready:
                            if parsed_rate:
                                sample_rate = parsed_rate
                            if parsed_ch:
                                num_channels = parsed_ch
                            bytes_per_chunk = (
                                int(sample_rate * (chunk_duration_ms / 1000.0))
                                * bytes_per_sample
                                * num_channels
                            )
                    if not header_ready:
                        continue
                    while len(leftover) >= bytes_per_chunk:
                        piece = bytes(leftover[:bytes_per_chunk])
                        del leftover[:bytes_per_chunk]
                        await self._push_pcm_frame(piece, sample_rate, num_channels, bytes_per_sample)
                        await asyncio.sleep(chunk_duration_ms / 1000.0)
                if leftover:
                    await self._push_pcm_frame(bytes(leftover), sample_rate, num_channels, bytes_per_sample)
                logger.info(
                    f"Streaming TTS published to the room ({received} bytes received)."
                )
            except Exception as e:
                logger.warning(
                    f"TTS stream dropped after {received} bytes: {e}; "
                    "playing whatever partial audio was already captured"
                )
                if leftover:
                    try:
                        await self._push_pcm_frame(
                            bytes(leftover), sample_rate, num_channels, bytes_per_sample
                        )
                    except Exception as flush_err:
                        logger.warning(f"Failed to flush partial TTS PCM: {flush_err}")
            finally:
                self._agent_playback_ended_at = time.time()

    async def play_audio_pcm(self, raw_pcm: bytes, sample_rate: int, num_channels: int, is_real: bool = False):
        async with self.playback_lock:
            import time
            ts = time.time()
            if is_real:
                logger.info(f"[{ts:.3f}] real_response_playback_start (chunking 20ms)")
            else:
                logger.info(f"[{ts:.3f}] filler_playback_lock_acquired (chunking 20ms)")
                
            bytes_per_sample = 2  # Assuming 16-bit PCM
            chunk_duration_ms = 20
            samples_per_chunk = int(sample_rate * (chunk_duration_ms / 1000.0))
            bytes_per_chunk = samples_per_chunk * bytes_per_sample * num_channels
            
            try:
                for i in range(0, len(raw_pcm), bytes_per_chunk):
                    chunk_data = raw_pcm[i:i + bytes_per_chunk]
                    chunk_samples = len(chunk_data) // (bytes_per_sample * num_channels)
                    
                    frame = rtc.AudioFrame(
                        data=chunk_data,
                        sample_rate=sample_rate,
                        num_channels=num_channels,
                        samples_per_channel=chunk_samples,
                    )
                    await self.audio_source.capture_frame(frame)
                    
                    # Sleep to pace the audio pushing natively matching the stream rate
                    await asyncio.sleep(chunk_duration_ms / 1000.0)
                    
                logger.info("Audio successfully published to the room and finished playing.")
            except Exception as e:
                logger.error(f"Failed to play audio PCM chunk: {e}")
            finally:
                self._agent_playback_ended_at = time.time()

    async def play_audio(self, audio_data: bytes):
        logger.info("real_response_ready")
        try:
            with wave.open(io.BytesIO(audio_data), "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
                sample_rate = wf.getframerate()
                num_channels = wf.getnchannels()

            # No more silence padding, use the unified 20ms chunking method
            await self.play_audio_pcm(raw_pcm, sample_rate, num_channels, is_real=True)
        except Exception as e:
            logger.error(f"Failed to process audio data: {e}")
