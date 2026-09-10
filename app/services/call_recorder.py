import os
import asyncio
import wave
import logging
import traceback
from livekit import rtc

logger = logging.getLogger("call_recorder")

_recording_tasks = {}

async def _record_track(track: rtc.Track, filepath: str):
    logger.info(f"Starting PCM capture to {filepath}")
    audio_stream = None
    try:
        audio_stream = rtc.AudioStream(track)
        
        # We need to wait for the first frame to know the sample rate and num_channels
        first_frame_event = await audio_stream.__anext__()
        first_frame = first_frame_event.frame
        
        sample_rate = first_frame.sample_rate
        num_channels = first_frame.num_channels
        
        # open wave file
        with wave.open(filepath, "wb") as wav_file:
            wav_file.setnchannels(num_channels)
            # LiveKit AudioFrames are 16-bit PCM (2 bytes per sample)
            wav_file.setsampwidth(2) 
            wav_file.setframerate(sample_rate)
            
            # Write first frame
            wav_file.writeframes(first_frame.data.tobytes())
            
            # Continue reading frames
            async for event in audio_stream:
                wav_file.writeframes(event.frame.data.tobytes())
                
    except asyncio.CancelledError:
        logger.info(f"Recording task cancelled cleanly for {filepath}")
    except Exception as e:
        logger.error(f"Recording task failed: {e}")
        logger.debug(traceback.format_exc())
    finally:
        if audio_stream:
            # Need to close the stream safely
            # wait, aclose() is for async generator, AudioStream in LiveKit may not have it
            # wait, rtc.AudioStream usually can be iterated. Let's just let it be.
            try:
                if hasattr(audio_stream, 'aclose'):
                    await audio_stream.aclose()
            except Exception:
                pass
        logger.info(f"Finished PCM capture to {filepath}")

def start_recording(room: rtc.Room, session_id: str):
    recordings_dir = os.getenv("RECORDINGS_DIR", "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    filepath = os.path.join(recordings_dir, f"{session_id}.wav")

    # Check for already subscribed audio tracks
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                if session_id not in _recording_tasks:
                    logger.info(f"Attaching recording to existing track for {session_id}")
                    task = asyncio.create_task(_record_track(pub.track, filepath))
                    _recording_tasks[session_id] = task

    @room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            if session_id in _recording_tasks:
                return
            logger.info(f"Attaching recording on track_subscribed for {session_id}")
            task = asyncio.create_task(_record_track(track, filepath))
            _recording_tasks[session_id] = task

async def stop_recording(session_id: str):
    if session_id in _recording_tasks:
        task = _recording_tasks.pop(session_id)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for recording task {session_id} to finish")
        except asyncio.CancelledError:
            pass
