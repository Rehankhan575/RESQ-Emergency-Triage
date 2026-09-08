import asyncio
import os
import sys

# Ensure app is in PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.agents.voice_agent import synthesize_tts

FILLERS = [
    "Haan, sun raha hoon",
    "Ji, bataiye",
    "Theek hai",
    "Samajh gaya",
    "Ji, boliye"
]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'app', 'assets', 'fillers')

async def generate_all():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for i, text in enumerate(FILLERS, start=1):
        filename = f"filler_{i}.wav"
        filepath = os.path.join(OUTPUT_DIR, filename)
        print(f"Generating {filename} -> '{text}'...")
        try:
            audio_bytes = await synthesize_tts(text)
            with open(filepath, "wb") as f:
                f.write(audio_bytes)
            print(f"Saved {filepath} ({len(audio_bytes)} bytes)")
        except Exception as e:
            print(f"Failed to generate {filename}: {e}")

if __name__ == "__main__":
    asyncio.run(generate_all())
