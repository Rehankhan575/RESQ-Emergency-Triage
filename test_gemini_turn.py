import asyncio
import json
from app.agents.triage.agent import process_turn

async def run():
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

    print("Testing Turn...")
    success, state = await process_turn(transcript, history, previous_state=prev_state)
    print(f"Success: {success}")
    if not success:
        print("Failed!")
    else:
        print(json.dumps(state, indent=2))

asyncio.run(run())
