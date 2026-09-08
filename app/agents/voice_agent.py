import os
import time
import asyncio
import logging
import httpx
import base64
import wave
import io
import random
from typing import Optional
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.plugins import deepgram

from app.agents.triage.agent import process_turn

load_dotenv()
logger = logging.getLogger("voice_agent")
logger.setLevel(logging.INFO)

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# ── Persistent HTTP client (Fix 4) ──────────────────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


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
        self.filler_frames: list[rtc.AudioFrame] = []
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
                            frame = rtc.AudioFrame(
                                data=raw_pcm,
                                sample_rate=sample_rate,
                                num_channels=num_channels,
                                samples_per_channel=len(raw_pcm) // (2 * num_channels),
                            )
                            self.filler_frames.append(frame)
                if not self.filler_frames:
                    logger.warning("filler_load_failed: No valid wav files found, continuing without filler playback")
                else:
                    logger.info(f"Loaded {len(self.filler_frames)} filler clips")
        except Exception as e:
            logger.warning(f"filler_load_failed: {e}, continuing without filler playback")
            self.filler_frames = []

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

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        logger.info("Agent started. Publishing audio track...")
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.ctx.room.local_participant.publish_track(self.audio_track, options)
        logger.info("Agent audio track published successfully.")

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

        # Greet caller
        greeting = "Namaste, main aapki madad ke liye yahan hoon. Kripya apni emergency bataayein."
        await self.say(greeting)

    # ── STT pipeline ─────────────────────────────────────────────────────────

    async def process_audio_track(self, track: rtc.RemoteAudioTrack):
        logger.info("STT stream processing started for user audio track.")
        audio_stream = rtc.AudioStream(track)
        stt_stream = self.stt.stream()

        pending_transcript_buffer = []
        pending_debounce_task: asyncio.Task | None = None
        last_speech_end_time = 0.0
        last_receipt_time = 0.0

        async def push_frames():
            async for frame_event in audio_stream:
                stt_stream.push_frame(frame_event.frame)

        async def read_events():
            nonlocal pending_debounce_task, last_speech_end_time, last_receipt_time
            
            async def trigger_turn():
                full_transcript = " ".join(pending_transcript_buffer)
                pending_transcript_buffer.clear()
                
                logger.info("transcript_finalized")

                # enqueue full aggregated transcript
                await self.turn_queue.put(full_transcript)
                
            async def debounce_waiter():
                await asyncio.sleep(2.0)
                if pending_transcript_buffer:
                    await trigger_turn()

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
                        
                        # If this is the FIRST fragment of a turn, trigger filler
                        if not pending_transcript_buffer:
                            self.filler_played_this_turn = False
                            if self.filler_frames:
                                if not self.filler_played_this_turn:
                                    self.filler_played_this_turn = True
                                    available = [i for i in range(len(self.filler_frames)) if i != self.last_filler_index]
                                    if not available:
                                        available = [0]
                                    self.last_filler_index = random.choice(available)
                                    frame = self.filler_frames[self.last_filler_index]
                                    logger.info("filler_playback_start")
                                    asyncio.create_task(self.play_audio_frame(frame, is_real=False))
                            else:
                                logger.info("filler_skipped_empty")

                        pending_transcript_buffer.append(transcript.strip())

                        # Broadcast user transcript immediately — don't wait for LLM
                        await broadcast_to_backend(
                            self.session_id,
                            "transcript_chunk",
                            {"speaker": "user", "text": transcript},
                        )
                        
                        if pending_debounce_task and not pending_debounce_task.done():
                            pending_debounce_task.cancel()
                        pending_debounce_task = asyncio.create_task(debounce_waiter())

        try:
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
            logger.info(f"LLM Turn Handler Invocation #{turn_seq}: Sending transcript exactly as: '{transcript}'")
            try:
                await self.on_user_speech(transcript)
            except Exception as e:
                logger.error(f"Error processing queued turn: {e}")
            finally:
                self.turn_queue.task_done()

    # ── Core turn handler ─────────────────────────────────────────────────────

    async def on_user_speech(self, transcript: str):
        logger.info(f"Processing LLM Turn for: '{transcript}'")

        # Fix 1: snapshot history BEFORE this turn, then append transcript
        history_before_turn = list(self.history)
        self.history.append(transcript)

        # Fix 1: single task with a soft-deadline filler, NOT cancel-and-restart
        task = asyncio.create_task(
            process_turn(transcript, history_before_turn, previous_state=self.current_state)
        )
        success, result = False, {}
        try:
            done, _ = await asyncio.wait({task}, timeout=4.0)
            if task in done:
                success, result = task.result()
            else:
                # Soft deadline exceeded — play filler while waiting for the result
                await self.say("Ek second, main check kar raha hoon.")
                success, result = await asyncio.wait_for(task, timeout=8.0)
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
        self.current_state["flag_for_human"] = True
        await broadcast_to_backend(
            self.session_id, "triage_update", self.current_state
        )
        escalation_msg = "Maaf karein, main aapko human operator se jod raha hoon."
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
            audio_data = await synthesize_tts(text, voice="priya")
            logger.info("TTS synthesis completed. Publishing audio to room...")
            await self.play_audio(audio_data)
        except Exception as e:
            logger.error(f"TTS API Error: {e}")

    async def play_audio_frame(self, frame: rtc.AudioFrame, is_real: bool = False):
        async with self.playback_lock:
            if is_real:
                logger.info("real_response_playback_start")
            else:
                logger.info("filler_playback_lock_acquired")
            try:
                await self.audio_source.capture_frame(frame)
                # Ensure the lock genuinely represents "audio finished playing"
                duration = frame.samples_per_channel / frame.sample_rate
                await asyncio.sleep(duration)
                logger.info("Audio successfully published to the room and finished playing.")
            except Exception as e:
                logger.error(f"Failed to play audio frame: {e}")

    async def play_audio(self, audio_data: bytes):
        logger.info("real_response_ready")
        try:
            with wave.open(io.BytesIO(audio_data), "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
                sample_rate = wf.getframerate()
                num_channels = wf.getnchannels()

            frame = rtc.AudioFrame(
                data=raw_pcm,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=len(raw_pcm) // (2 * num_channels),
            )
            await self.play_audio_frame(frame, is_real=True)
        except Exception as e:
            logger.error(f"Failed to process audio data: {e}")
