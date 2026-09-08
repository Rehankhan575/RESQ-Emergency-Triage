import os

filepath = "/Users/rehan/Desktop/emergency_triage/sih_triage/app/agents/voice_agent.py"
with open(filepath, "r") as f:
    content = f.read()

# 1. Imports
content = content.replace("import io\nfrom typing import Optional", "import io\nimport random\nfrom typing import Optional")

# 2. __init__
init_target = """    def __init__(self, ctx: agents.JobContext, session_id: str = "unknown"):
        self.ctx = ctx
        self.session_id = session_id

        # Conversation history — alternating user / model strings"""

init_replace = """    def __init__(self, ctx: agents.JobContext, session_id: str = "unknown"):
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

        # Conversation history — alternating user / model strings"""
content = content.replace(init_target, init_replace)

# 3. read_events
events_target = """                    if transcript.strip():
                        logger.info(f"STT Transcript Received: {transcript}")
                        # Broadcast user transcript immediately — don't wait for LLM"""

events_replace = """                    if transcript.strip():
                        logger.info(f"STT Transcript Received: {transcript}")
                        logger.info("transcript_finalized")

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

                        # Broadcast user transcript immediately — don't wait for LLM"""
content = content.replace(events_target, events_replace)

# 4. _turn_worker
worker_target = """    async def _turn_worker(self):
        \"\"\"Drains turn_queue sequentially so STT is never blocked by LLM/TTS.\"\"\"
        while True:
            transcript = await self.turn_queue.get()
            try:"""

worker_replace = """    async def _turn_worker(self):
        \"\"\"Drains turn_queue sequentially so STT is never blocked by LLM/TTS.\"\"\"
        while True:
            transcript = await self.turn_queue.get()
            self.filler_played_this_turn = False
            try:"""
content = content.replace(worker_target, worker_replace)

# 5. play_audio
play_target = """    async def play_audio(self, audio_data: bytes):
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
            await self.audio_source.capture_frame(frame)
            logger.info("Audio successfully published to the room.")
        except Exception as e:
            logger.error(f"Failed to play audio frame: {e}")"""

play_replace = """    async def play_audio_frame(self, frame: rtc.AudioFrame, is_real: bool = False):
        if is_real:
            logger.info("real_response_playback_start")
        async with self.playback_lock:
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
            logger.error(f"Failed to process audio data: {e}")"""
content = content.replace(play_target, play_replace)

with open(filepath, "w") as f:
    f.write(content)
print("Done!")
