import asyncio
from livekit import rtc

async def publish_dummy_audio(room: rtc.Room):
    source = rtc.AudioSource(sample_rate=48000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("dummy_mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    publication = await room.local_participant.publish_track(track, options)
    
    # Keep pushing silence frames so Egress/Recorder has something to record!
    async def push_frames():
        # 10ms frame at 48kHz = 480 samples
        frame = rtc.AudioFrame.create(48000, 1, 480)
        # zeroed out (silence)
        for i in range(480):
            frame.data[i] = 0
            
        while True:
            await source.capture_frame(frame)
            await asyncio.sleep(0.01)
            
    asyncio.create_task(push_frames())
