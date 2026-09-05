import json
import asyncio
import logging
import time
from typing import List, Dict, Any, Tuple
from pydantic import ValidationError

from google import genai
from google.genai import types

from app.models.triage import TriageState, SeverityEnum, EmergencyTypeEnum
from app.core.config import settings

logger = logging.getLogger("voice_agent")

client = genai.Client(api_key=settings.GEMINI_API_KEY)

MODEL_NAME = "gemini-3.6-flash"


async def warm_up_gemini_connection() -> None:
    """
    Fire a trivial no-op request on the module-level `client` singleton so the
    underlying TLS/HTTP connection is established and pooled BEFORE any real
    caller arrives.  This is pure best-effort: a failure must never crash the
    worker or block it from accepting calls.
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        chat = client.aio.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=4,
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            ),
        )
        resp = await chat.send_message("ping")
        elapsed = _time.perf_counter() - t0
        logger.info(f"Gemini connection warmed in {elapsed:.2f}s (response: {str(resp.text)[:20]!r})")
    except Exception as exc:
        elapsed = _time.perf_counter() - t0
        logger.warning(f"Gemini warm-up failed after {elapsed:.2f}s: {exc}")


class TriagePrompt:
    @staticmethod
    def get_system_prompt() -> str:
        return """You are an AI Voice Emergency Triage Assistant. Your role is to accurately assess emergency situations reported by callers, extract critical information, and guide the conversation logically.

CRITICAL RULES:
1. State Machine & Question Ordering:
   You must extract and track these critical fields: `location`, `emergency_type`, `injuries`, `people_affected`.
   - If `location` is null, ask for the address/location.
   - If `location` is filled but `emergency_type` is null, ask what the emergency is.
   - If `location` and `emergency_type` are filled but `injuries` is null, ask if anyone is injured.
   - If `location`, `emergency_type`, and `injuries` are filled but `people_affected` is null, ask how many people are affected.
   - NEVER ask for a field that is already filled based on the conversation history. Keep previously filled fields in your JSON output.

2. Conversation Completion:
   - If ALL critical fields (`location`, `emergency_type`, `injuries`, `people_affected`) are filled, you MUST set `next_question` = "Aapka call record kar liya gaya hai. Hum aapki madad bhej rahe hain." and set `flag_for_human` = False.

3. Escalation Rules:
   - Only set `flag_for_human` = True if CONFIDENCE < 0.5 AND the conversation has gone on for at least 3 turns (meaning you are repeatedly failing to get info), OR if the user explicitly demands a human or operator.

4. Calming Panicking Callers:
   - Infer `caller_stress_level` from the user's text (panicking, anxious, calm).
   - If the user is "panicking" (using urgent words, shouting, very scared), you MUST prefix your `next_question` with EXACTLY: "Kripya shant ho jayein. Main aapki madad kar raha hoon. " before asking the next question.

5. Output Format:
   - You MUST respond in VALID JSON format matching the `TriageState` schema EXACTLY.
   - Schema structure:
     {
       "location": "string or null",
       "emergency_type": "FIRE", "MEDICAL", "ACCIDENT", "FLOOD", "CRIME", or "OTHER" (or null),
       "people_affected": integer or null,
       "injuries": boolean or null,
       "severity": "LOW", "MEDIUM", "HIGH", or "CRITICAL" (or null),
       "confidence": float between 0.0 and 1.0,
       "flag_for_human": boolean,
       "next_question": "string (the next question to ask in Hindi/Hinglish)",
       "caller_stress_level": "calm", "anxious", or "panicking",
       "reasoning": "string (A single sentence, max ~25 words. Explain specifically what caused your confidence level — cite what was clear or unclear in the caller's speech, not what fields you filled in.)"
     }
"""


async def process_turn(
    user_transcript: str,
    history_before_turn: List[str],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Process the current turn of the conversation.

    Args:
        user_transcript: The latest transcript from the user (current turn).
        history_before_turn: Alternating user/model strings from BEFORE this
            turn. Caller is responsible for passing a clean pre-turn snapshot
            (no trimming is performed here).

    Returns:
        (True, triage_state_dict)  on success.
        (False, {})                on any failure — no fallback state is
                                   fabricated here; policy lives in the caller.
    """
    try:
        # Build Gemini-format history from the pre-turn snapshot
        gemini_history = []
        for i, text in enumerate(history_before_turn):
            role = "user" if i % 2 == 0 else "model"
            gemini_history.append({"role": role, "parts": [{"text": text}]})

        logger.info(f"Gemini History Context: {gemini_history}")

        chat = client.aio.chats.create(
            model=MODEL_NAME,
            history=gemini_history,
            config=types.GenerateContentConfig(
                system_instruction=TriagePrompt.get_system_prompt(),
                temperature=0.3,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            ),
        )

        response = await chat.send_message(user_transcript)
        parsed_json = json.loads(response.text)

        # Validate against TriageState Pydantic model
        triage_state = TriageState(**parsed_json)
        return True, triage_state.model_dump()

    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Validation or Parsing Error: {e}")
        return False, {}

    except Exception as e:
        logger.warning(f"API Error: {e}")
        return False, {}


if __name__ == "__main__":
    TEST_CASES = [
        {
            "label": "Test 1: Clear speech",
            "transcript": "Mere ghar mein aag lag gayi, please help karo!!!",
            "history": [],
        },
        {
            "label": "Test 2: Garbled speech",
            "transcript": "Hello? Bzzzt krrr... I don't know... bzzz",
            "history": [
                "Hello, kripya apna emergency batayein."
            ],
        },
    ]

    async def run_tests():
        print(f"=== Triage Agent Test — Model: {MODEL_NAME} ===\n")
        for tc in TEST_CASES:
            print(f"--- {tc['label']} ---")
            print(f"  Transcript : {tc['transcript']}")
            print(f"  History len: {len(tc['history'])} turns")

            t0 = time.perf_counter()
            success, result = await process_turn(tc["transcript"], tc["history"])
            elapsed = time.perf_counter() - t0

            print(f"  Success    : {success}")
            print(f"  Latency    : {elapsed:.3f}s")
            if success:
                print(f"  State      : {json.dumps(result, default=str, indent=4)}")
            else:
                print(f"  Result     : (empty dict — failure)")
            print()

    asyncio.run(run_tests())
