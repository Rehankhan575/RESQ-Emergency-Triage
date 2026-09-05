"""
Standalone warm-up verification: fires warm_up_gemini_connection() then
immediately makes a real process_turn() call to measure post-warm latency.
Uses the EXACT same client singleton from app/agents/triage/agent.py.
"""
import asyncio
import time
import json
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))

# Import the REAL module-level singleton client and functions
from app.agents.triage.agent import (
    client,          # the genai.Client singleton
    MODEL_NAME,
    warm_up_gemini_connection,
    process_turn,
)
from google.genai import types


async def main():
    print("=== Gemini warm-up + first-turn latency test ===")
    print(f"Using client id: {id(client)}")
    print()

    # Step 1: warm-up (same function that worker.py calls)
    print("[WARM-UP]  firing...")
    await warm_up_gemini_connection()

    # Step 2: simulate ~1s of LiveKit room setup
    print("[PAUSE]    sleeping 1s to simulate room setup...")
    await asyncio.sleep(1.0)

    # Step 3: first real process_turn() — should now be fast
    print("[TURN 1]   calling process_turn() with cold history...")
    t0 = time.perf_counter()
    success, result = await process_turn(
        "Mere ghar mein aag lag gayi, please help karo!!!",
        history_before_turn=[],
    )
    elapsed = time.perf_counter() - t0

    print(f"           success={success}  latency={elapsed:.3f}s")
    if success:
        print(f"           next_question={result.get('next_question', '')!r}")

    print()
    baseline = 7.978
    improvement = baseline - elapsed
    print(f"Baseline (cold, unwarmed): {baseline:.3f}s")
    print(f"After warm-up:             {elapsed:.3f}s")
    if improvement > 0:
        print(f"Improvement:               +{improvement:.3f}s ({improvement/baseline*100:.0f}% faster)")
    else:
        print(f"No improvement observed:   {elapsed:.3f}s (still >{baseline:.3f}s)")
        print("  → Bottleneck may NOT be connection warming. Investigate further.")


asyncio.run(main())
