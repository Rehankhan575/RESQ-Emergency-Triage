import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.agents.voice_agent import synthesize_tts

async def gen():
    print("Generating long audio...")
    audio_bytes = await synthesize_tts("Kripya line par bane rahein, main aapki jaankari darj kar raha hoon. Kripya thoda intezaar karein, main system check kar raha hoon.")
    with open("app/assets/fillers/filler_0.wav", "wb") as f:
        f.write(audio_bytes)
    print("Saved super long filler to filler_0.wav")

if __name__ == "__main__":
    asyncio.run(gen())
