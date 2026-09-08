import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch, MagicMock

# Configure logging format so voice_agent and triage agent logs are clearly displayed
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("test_prank_exit")

from app.agents.voice_agent import TriageVoiceAgent


async def run_scenario_a():
    print("\n" + "="*80)
    print("SCENARIO A: Caller says 'ye ek prank call hai' as first message (No prior emergency)")
    print("="*80 + "\n")

    mock_ctx = MagicMock()
    mock_ctx.room = MagicMock()
    agent = TriageVoiceAgent(ctx=mock_ctx, session_id="test-prank-scenario-a")
    agent.say = AsyncMock()

    # LLM recognizes caller explicitly states prank with no emergency
    turn1_llm_response = {
        "location": None,
        "emergency_type": None,
        "people_affected": None,
        "injuries": None,
        "severity": None,
        "confidence": 0.95,
        "flag_for_human": False,
        "next_question": "Kya ye prank hai?",
        "caller_stress_level": "calm",
        "reasoning": "Caller explicitly claimed this is a prank call",
        "is_prank": True
    }

    mock_chat = MagicMock()
    mock_chat.send_message = AsyncMock(return_value=MagicMock(text=json.dumps(turn1_llm_response)))

    with patch("google.genai.chats.AsyncChats.create", return_value=mock_chat), \
         patch("app.agents.voice_agent.broadcast_to_backend", new=AsyncMock()):

        transcript = "ye ek prank call hai"
        print(f">>> [CALL FLOW] Caller speaks: '{transcript}'")
        await agent.on_user_speech(transcript)

        print("\n>>> Resulting State for Scenario A:")
        print(f"  is_prank       : {agent.current_state.get('is_prank')}")
        print(f"  flag_for_human : {agent.current_state.get('flag_for_human')}")
        print(f"  severity       : {agent.current_state.get('severity')}")
        print(f"  emergency_type : {agent.current_state.get('emergency_type')}")
        print(f"  next_question  : '{agent.current_state.get('next_question')}'")

        closing = "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye."
        assert agent.current_state.get("is_prank") is True, "FAIL: is_prank should be True!"
        assert agent.current_state.get("flag_for_human") is False, "FAIL: flag_for_human should be False for clean early exit!"
        assert agent.current_state.get("next_question") == closing, (
            f"FAIL: next_question must be the exact closing line, got {agent.current_state.get('next_question')!r}"
        )
        print("\n>>> [SCENARIO A PASSED]: Early exit cleanly triggered with no further questions!")


async def run_scenario_b():
    print("\n" + "="*80)
    print("SCENARIO B: Real emergency reported first, then caller says 'just kidding lol'")
    print("="*80 + "\n")

    mock_ctx = MagicMock()
    mock_ctx.room = MagicMock()
    agent = TriageVoiceAgent(ctx=mock_ctx, session_id="test-prank-scenario-b")
    agent.say = AsyncMock()

    # Turn 1: Real emergency reported
    turn1_llm_response = {
        "location": "Laxmi Nagar, Delhi",
        "emergency_type": "FIRE",
        "people_affected": 2,
        "injuries": True,
        "severity": "HIGH",
        "confidence": 0.90,
        "flag_for_human": False,
        "next_question": "Laxmi Nagar mein aag lagi hai, kya koi andar fasa hai?",
        "caller_stress_level": "anxious",
        "reasoning": "Report of building fire with injuries",
        "is_prank": False
    }

    # Turn 2: Caller claims prank / tries to back out
    turn2_llm_response = {
        "location": "Laxmi Nagar, Delhi",
        "emergency_type": "FIRE",
        "people_affected": 2,
        "injuries": True,
        "severity": "HIGH",
        "confidence": 0.85,
        "flag_for_human": False,       # LLM might attempt False because caller claims prank
        "next_question": "Theek hai, dhanyavaad.", # LLM might attempt to close
        "caller_stress_level": "calm",
        "reasoning": "Caller claims they were just kidding",
        "is_prank": True
    }

    mock_chat = MagicMock()
    mock_chat.send_message = AsyncMock(side_effect=[
        MagicMock(text=json.dumps(turn1_llm_response)),
        MagicMock(text=json.dumps(turn2_llm_response)),
    ])

    with patch("google.genai.chats.AsyncChats.create", return_value=mock_chat), \
         patch("app.agents.voice_agent.broadcast_to_backend", new=AsyncMock()):

        # Turn 1
        t1 = "aag lagi hai, ek insaan ghayal hai"
        print(f">>> [TURN 1] Caller speaks: '{t1}'")
        await agent.on_user_speech(t1)
        print(f"Turn 1 State: emergency_type={agent.current_state.get('emergency_type')}, injuries={agent.current_state.get('injuries')}, severity={agent.current_state.get('severity')}, flag_for_human={agent.current_state.get('flag_for_human')}")
        assert agent.current_state.get("emergency_type") == "FIRE"
        assert agent.current_state.get("injuries") is True

        # Turn 2
        t2 = "just kidding lol"
        print(f"\n>>> [TURN 2] Caller speaks: '{t2}'")
        print(">>> Watch for the [WARNING] log from check_prank_exit blocking early-exit on the LIVE call path:")
        await agent.on_user_speech(t2)

        print("\n>>> Resulting State for Scenario B:")
        print(f"  is_prank       : {agent.current_state.get('is_prank')}")
        print(f"  flag_for_human : {agent.current_state.get('flag_for_human')}")
        print(f"  severity       : {agent.current_state.get('severity')}")
        print(f"  emergency_type : {agent.current_state.get('emergency_type')}")
        print(f"  next_question  : '{agent.current_state.get('next_question')}'")

        closing = "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye."
        assert agent.current_state.get("is_prank") is True, "FAIL: is_prank should remain True as logged flag!"
        assert agent.current_state.get("flag_for_human") is True, "FAIL: flag_for_human MUST be forced to True when prior emergency exists!"
        assert agent.current_state.get("next_question") != closing, "FAIL: Early exit must NOT be triggered after a real distress signal!"
        assert "phir se call" not in (agent.current_state.get("next_question") or ""), "FAIL: Should NOT have silently closed with exit line!"
        print("\n>>> [SCENARIO B PASSED]: Guard blocked early exit and forced flag_for_human=True for human review!")


async def main():
    await run_scenario_a()
    await run_scenario_b()


if __name__ == "__main__":
    asyncio.run(main())
