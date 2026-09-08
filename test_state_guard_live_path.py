import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch, MagicMock

# Configure logging format so voice_agent and triage agent WARNING/INFO logs are clearly visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("test_live_path")

from app.agents.voice_agent import TriageVoiceAgent


async def run_live_path_test():
    print("================================================================================")
    print("STARTING LIVE-PATH TEST: 3-Turn Call Flow via TriageVoiceAgent.on_user_speech()")
    print("================================================================================\n")

    # Mock JobContext so LiveKit room connection isn't required for this test
    mock_ctx = MagicMock()
    mock_ctx.room = MagicMock()

    agent = TriageVoiceAgent(ctx=mock_ctx, session_id="test-session-live-path-001")

    # Mock TTS and audio output so external Sarvam API/audio output aren't invoked
    agent.say = AsyncMock()
    
    # ── TURN 1: Initial report (Location + Incident) ───────────────────────────
    turn1_llm_response = {
        "location": "Gandhi Nagar, Delhi",
        "emergency_type": "FIRE",
        "people_affected": 2,
        "injuries": False,
        "severity": "HIGH",
        "confidence": 0.85,
        "flag_for_human": False,
        "next_question": "Gandhi Nagar mein aag lagi hai, kya koi andar fasa hai?",
        "caller_stress_level": "anxious",
        "reasoning": "Report of building fire in Gandhi Nagar"
    }

    # ── TURN 2: Escalation to CRITICAL + flag_for_human=True ───────────────────
    turn2_llm_response = {
        "location": "Gandhi Nagar, Delhi",
        "emergency_type": "FIRE",
        "people_affected": 5,
        "injuries": True,
        "severity": "CRITICAL",
        "confidence": 0.95,
        "flag_for_human": True,
        "next_question": "Madad turant bheji ja rahi hai, line par rahiye.",
        "caller_stress_level": "panicking",
        "reasoning": "Severe fire with people trapped and injured"
    }

    # ── TURN 3: Flaky/Hallucinating LLM tries to DOWNGRADE both ───────────────
    # Tries to set flag_for_human=False and severity=LOW
    turn3_llm_response = {
        "location": "Gandhi Nagar, Delhi",
        "emergency_type": "FIRE",
        "people_affected": 5,
        "injuries": True,
        "severity": "LOW",              # <--- ATTEMPTED ILLEGAL DOWNGRADE from CRITICAL
        "confidence": 0.9,
        "flag_for_human": False,         # <--- ATTEMPTED ILLEGAL REVERT from True
        "next_question": "Kya sab theek hai abhi?",
        "caller_stress_level": "calm",
        "reasoning": "Caller sounds calmer now"
    }

    mock_chat = MagicMock()
    mock_chat.send_message = AsyncMock(side_effect=[
        MagicMock(text=json.dumps(turn1_llm_response)),
        MagicMock(text=json.dumps(turn2_llm_response)),
        MagicMock(text=json.dumps(turn3_llm_response)),
    ])

    with patch("google.genai.chats.AsyncChats.create", return_value=mock_chat), \
         patch("app.agents.voice_agent.broadcast_to_backend", new=AsyncMock()):

        # --- TURN 1 ---
        print("\n>>> [CALL FLOW] EXECUTING TURN 1...")
        transcript_turn1 = "Main Gandhi Nagar se bol raha hoon, yahan aag lag gayi hai!"
        await agent.on_user_speech(transcript_turn1)
        print(f"Turn 1 Resulting State: severity={agent.current_state.get('severity')}, flag_for_human={agent.current_state.get('flag_for_human')}")

        # --- TURN 2 ---
        print("\n>>> [CALL FLOW] EXECUTING TURN 2 (escalating to CRITICAL + flag_for_human=True)...")
        transcript_turn2 = "Bohot bhayanak aag hai, 5 log fase hain aur 2 log behosh hain!"
        await agent.on_user_speech(transcript_turn2)
        print(f"Turn 2 Resulting State: severity={agent.current_state.get('severity')}, flag_for_human={agent.current_state.get('flag_for_human')}")
        assert agent.current_state.get("severity") == "CRITICAL", f"Expected CRITICAL, got {agent.current_state.get('severity')}"
        assert agent.current_state.get("flag_for_human") is True, f"Expected flag_for_human=True, got {agent.current_state.get('flag_for_human')}"

        # --- TURN 3 ---
        print("\n>>> [CALL FLOW] EXECUTING TURN 3 (LLM hallucinates downgrade to LOW and flag_for_human=False)...")
        print(">>> Watch for the [WARNING] override logs from validate_state_transition() on this live path:")
        transcript_turn3 = "Hum dusri manzil par hain, smoke bohot badh raha hai!"
        await agent.on_user_speech(transcript_turn3)

        print("\n>>> [CALL FLOW] TURN 3 FINISHED.")
        print(f"Turn 3 Resulting State: severity={agent.current_state.get('severity')}, flag_for_human={agent.current_state.get('flag_for_human')}")

        # Verify enforcement
        assert agent.current_state.get("flag_for_human") is True, "FAIL: flag_for_human was downgraded!"
        assert agent.current_state.get("severity") == "CRITICAL", "FAIL: severity was downgraded from CRITICAL!"
        print("\n>>> [TEST SUCCESS] Guard successfully protected state on the live call path!")


if __name__ == "__main__":
    asyncio.run(run_live_path_test())
