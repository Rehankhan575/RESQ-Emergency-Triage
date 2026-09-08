import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.agents.voice_agent import synthesize_tts

async def gen():
    audio_bytes = await synthesize_tts("Namaste, main ek test call kar raha hoon. Kya aap mujhe sun sakte hain?")
    with open("dummy.wav", "wb") as f:
        f.write(audio_bytes)
    print("Saved dummy.wav")

if __name__ == "__main__":
    asyncio.run(gen())
