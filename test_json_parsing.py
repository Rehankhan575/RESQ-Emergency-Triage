import asyncio
import time
from app.agents.triage.agent import process_turn

async def run_tests():
    print("Running 10 consecutive turns through process_turn to verify JSON parsing...\n")
    history = [
        "Namaste, main aapki madad ke liye yahan hoon. Kripya apni emergency bataayein.",
        "यहां गांधी नगर में आग लग गई है।",
        "Gandhi Nagar mein aag lagi hai, samajh gaya. Kya wahan kisi ko chot lagi hai?"
    ]
    transcript = "हां लगी है चोट चार लोगों को."
    
    prev_state = {
        "location": "Gandhi Nagar",
        "emergency_type": "FIRE",
        "people_affected": None,
        "injuries": None,
        "severity": "HIGH",
        "confidence": 0.95,
        "flag_for_human": False,
        "next_question": "Gandhi Nagar mein aag lagi hai, samajh gaya. Kya wahan kisi ko chot lagi hai?",
        "caller_stress_level": "anxious",
        "is_prank": False,
        "reasoning": "Location Gandhi Nagar and emergency_type FIRE are clearly stated."
    }

    failures = 0
    for i in range(1, 11):
        t0 = time.time()
        success, state = await process_turn(transcript, history, previous_state=prev_state)
        latency = time.time() - t0
        print(f"Turn {i}: Success={success} | Latency={latency:.2f}s")
        if not success:
            failures += 1
            print("  -> FAILED to parse JSON")
    
    print(f"\nTotal Failures: {failures}/10")

if __name__ == "__main__":
    asyncio.run(run_tests())
