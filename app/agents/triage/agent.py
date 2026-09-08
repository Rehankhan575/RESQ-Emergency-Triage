import json
import asyncio
import logging
import time
from typing import List, Dict, Any, Tuple, Optional
from pydantic import ValidationError

from google import genai
from google.genai import types

from app.models.triage import TriageState, SeverityEnum, EmergencyTypeEnum
from app.core.config import settings

logger = logging.getLogger("voice_agent")

client = genai.Client(api_key=settings.GEMINI_API_KEY)

MODEL_NAME = "gemini-2.5-flash"


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
        return """You are an AI Voice Emergency Triage Assistant. Your role is to accurately assess emergency situations reported by callers, extract critical information, and guide the conversation logically — while sounding like a calm, attentive human dispatcher, not a form.

CRITICAL RULES:

1. State Machine & Question Ordering:
   You must extract and track these critical fields: `location`, `emergency_type`, `injuries`, `people_affected`.
   - If `location` is null, ask for the address/location.
   - If `location` is filled but `emergency_type` is null, ask what the emergency is.
   - If `location` and `emergency_type` are filled but `injuries` is null, ask if anyone is injured.
   - If `location`, `emergency_type`, and `injuries` are filled but `people_affected` is null, ask how many people are affected.
   - NEVER ask for a field that is already filled based on the conversation history. Keep previously filled fields in your JSON output.

2. Acknowledgment Before Asking (MANDATORY):
   Before your next question, briefly acknowledge in 3-6 words what the caller just told you 
   (e.g. "Theek hai, samajh gaya", "Ji, noted", "Achha"). 
   NEVER ask a bare question with zero acknowledgment — this must feel like a real conversation.
   If the caller gave MULTIPLE fields in one turn, acknowledge all of them together before 
   moving to the next question (e.g. "Theek hai — [location] par [emergency_type], samjha. 
   Kya kisi ko chot lagi hai?").

3. Handling Unclear Answers:
   If the caller's answer for a field is unclear, garbled, or low-confidence, do NOT simply 
   repeat the same question. Paraphrase what you think you heard and ask them to confirm: 
   e.g. "Aapne [X] bola, sahi hai?" This must never come across as a stuck loop.

4. Conversation Completion:
   - If ALL critical fields (`location`, `emergency_type`, `injuries`, `people_affected`) are filled, 
     you MUST set `next_question` to a warm, specific closing line reflecting what was reported, e.g.: 
     "Aapki jaankari mil gayi hai — madad [location] ki taraf bheji ja rahi hai. Aap line par rahiye, hum saath hain."
     and set `flag_for_human` = False.

5. Escalation Rules:
   - Set `flag_for_human` = True if CONFIDENCE < 0.5 AND the conversation has gone on for at least 
     3 turns, OR if the user explicitly demands a human/operator, OR if `severity` is CRITICAL, 
     OR if injuries = true AND severity is HIGH or CRITICAL.

6. Calming Panicking Callers:
   - Infer `caller_stress_level` from the user's text (calm, anxious, panicking).
   - Only prepend a calming phrase the FIRST turn stress escalates to "panicking", or if it 
     escalates further. Do NOT repeat the identical phrase every turn — vary it if reassurance 
     is still needed:
     First panicking turn: "Kripya shant ho jayein. Main aapki madad kar raha hoon. "
     Later turns if still panicking: vary between "Aap theek karenge. ", "Madad aa rahi hai, 
     thoda dheeraj rakhein. "

7. Prank / False Alarm Early-Exit:
   - Set `is_prank` = True ONLY when the caller EXPLICITLY and unambiguously states this is a joke/prank/test call (e.g. "ye prank hai", "just joking", "test call tha", "maine mazak kiya"), not from ambiguous or sarcastic-sounding language alone. Otherwise, set `is_prank` = False.
   - If `is_prank` is True AND no emergency signals were reported, set `next_question` to: "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye." and `flag_for_human` = False.
   - If `is_prank` is claimed BUT emergency signals (type/injuries/severity) were already reported, set `is_prank` = True, set `flag_for_human` = True, and continue the triage flow.

8. Output Format:
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
       "next_question": "string (acknowledgment + next question, in natural Hindi/Hinglish, as a dispatcher would actually speak)",
       "caller_stress_level": "calm", "anxious", or "panicking",
       "reasoning": "string (max ~25 words, cite what was clear or unclear in the caller's speech)",
       "is_prank": boolean
     }
"""


def _get_state_attr(state: Any, key: str, default: Any = None) -> Any:
    if state is None:
        return default
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def validate_state_transition(
    previous_state: Any,
    new_state: Any,
) -> Any:
    """
    Enforces one-way safety invariants on LLM-produced state, independent of
    prompt compliance.

    Rules to enforce in code:
      1. If previous_state.flag_for_human == True, force new_state.flag_for_human = True
         regardless of what the LLM returned.
      2. If previous_state.severity == CRITICAL, force new_state.severity = CRITICAL
         regardless of what the LLM returned (no downgrade).
      3. If previous_state.severity == HIGH and new_state.severity == LOW in a single
         turn (large jump down), log a warning but don't block it.

    Logs a warning whenever this function actually corrects/overrides an LLM output,
    including previous value, what the LLM tried to output, and what was enforced instead.
    """
    if previous_state is None:
        return new_state

    is_dict = isinstance(new_state, dict)
    guarded = dict(new_state) if is_dict else new_state.model_copy()

    # Rule 1: flag_for_human one-way latch
    prev_flag = bool(_get_state_attr(previous_state, "flag_for_human", False))
    new_flag = bool(_get_state_attr(guarded, "flag_for_human", False))
    if prev_flag and not new_flag:
        logger.warning(
            f"validate_state_transition override: flag_for_human reverted by LLM. "
            f"[previous={prev_flag}, llm_output={new_flag}, enforced=True]"
        )
        if is_dict:
            guarded["flag_for_human"] = True
        else:
            guarded.flag_for_human = True

    # Normalize severity string representation for comparison
    prev_sev_raw = _get_state_attr(previous_state, "severity")
    prev_sev = prev_sev_raw.value if hasattr(prev_sev_raw, "value") else str(prev_sev_raw) if prev_sev_raw else None

    new_sev_raw = _get_state_attr(guarded, "severity")
    new_sev = new_sev_raw.value if hasattr(new_sev_raw, "value") else str(new_sev_raw) if new_sev_raw else None

    # Rule 2: CRITICAL severity non-downgrade
    if prev_sev == "CRITICAL" and new_sev is not None and new_sev != "CRITICAL":
        logger.warning(
            f"validate_state_transition override: severity downgrade attempted from CRITICAL. "
            f"[previous={prev_sev_raw}, llm_output={new_sev_raw}, enforced=CRITICAL]"
        )
        if is_dict:
            guarded["severity"] = "CRITICAL" if isinstance(prev_sev_raw, str) else SeverityEnum.CRITICAL
        else:
            guarded.severity = SeverityEnum.CRITICAL

    # Rule 3: HIGH -> LOW jump warning
    if prev_sev == "HIGH" and new_sev == "LOW":
        logger.warning(
            f"validate_state_transition notice: suspicious single-turn severity drop detected. "
            f"[previous={prev_sev_raw}, llm_output={new_sev_raw}, action=allowed_without_block]"
        )

    return guarded


def check_prank_exit(
    state: Any,
    previous_state: Optional[Any] = None,
) -> Any:
    """
    Evaluates early-exit safety for prank/false-alarm calls.

    Rules to enforce in code:
      1. If is_prank == True AND emergency_type is null AND injuries is null AND severity is null
         (i.e. no real emergency signal was ever captured in prior or current turn) ->
         allow early exit: set next_question to "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye."
         and flag_for_human = False.
      2. If is_prank == True BUT any of emergency_type/injuries/severity were ALREADY set to a
         non-null/concerning value in a PREVIOUS turn (before the prank claim), do NOT early-exit.
         Instead: keep is_prank=True as a logged flag, continue the normal question flow AND
         force flag_for_human = True.
    """
    if state is None:
        return state

    # Allow flexible argument ordering if invoked as check_prank_exit(prev, curr)
    if previous_state is not None and bool(_get_state_attr(previous_state, "is_prank", False)) and not bool(_get_state_attr(state, "is_prank", False)):
        target_state = previous_state
        prior_state = state
    else:
        target_state = state
        prior_state = previous_state

    is_prank = bool(_get_state_attr(target_state, "is_prank", False))
    if not is_prank:
        return target_state

    is_dict = isinstance(target_state, dict)
    guarded = dict(target_state) if is_dict else target_state.model_copy()

    def _is_set(val: Any) -> bool:
        if val is None:
            return False
        if isinstance(val, str) and val.strip().lower() in ("none", "null", ""):
            return False
        return True

    # Previous turn signals
    prev_etype = _get_state_attr(prior_state, "emergency_type") if prior_state else None
    prev_inj = _get_state_attr(prior_state, "injuries") if prior_state else None
    prev_sev = _get_state_attr(prior_state, "severity") if prior_state else None
    prev_has_signal = _is_set(prev_etype) or _is_set(prev_inj) or _is_set(prev_sev)

    # Current turn signals
    curr_etype = _get_state_attr(guarded, "emergency_type")
    curr_inj = _get_state_attr(guarded, "injuries")
    curr_sev = _get_state_attr(guarded, "severity")
    curr_has_signal = _is_set(curr_etype) or _is_set(curr_inj) or _is_set(curr_sev)

    if not prev_has_signal and not curr_has_signal:
        # Case A: Genuine prank / false alarm with no prior emergency signal
        closing_line = "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye."
        if is_dict:
            guarded["next_question"] = closing_line
            guarded["flag_for_human"] = False
        else:
            guarded.next_question = closing_line
            guarded.flag_for_human = False

        logger.info(
            f"check_prank_exit: Early exit allowed. No prior emergency signals detected "
            f"[is_prank=True, emergency_type=None, injuries=None, severity=None]. "
            f"Setting closing line and flag_for_human=False."
        )
    else:
        # Case B: Prank claimed BUT prior emergency signal exists -> Force flag_for_human=True
        if is_dict:
            guarded["is_prank"] = True
            guarded["flag_for_human"] = True
            closing_markers = ("phir se call", "dhanyavaad")
            curr_next_q = guarded.get("next_question", "")
            if any(m in curr_next_q.lower() for m in closing_markers):
                guarded["next_question"] = "Aapne pehle emergency report ki thi. Kripya line par bane rahein, human operator se connect kiya ja raha hai."
        else:
            guarded.is_prank = True
            guarded.flag_for_human = True
            closing_markers = ("phir se call", "dhanyavaad")
            if any(m in guarded.next_question.lower() for m in closing_markers):
                guarded.next_question = "Aapne pehle emergency report ki thi. Kripya line par bane rahein, human operator se connect kiya ja raha hai."

        logger.warning(
            f"check_prank_exit: Prank claimed BUT prior emergency signal exists! "
            f"Blocking early exit and forcing flag_for_human=True. "
            f"[is_prank=True, prev_emergency_type={prev_etype}, prev_injuries={prev_inj}, "
            f"prev_severity={prev_sev}, curr_emergency_type={curr_etype}, curr_injuries={curr_inj}, "
            f"curr_severity={curr_sev}]"
        )

    return guarded


async def process_turn(
    user_transcript: str,
    history_before_turn: List[str],
    previous_state: Optional[Any] = None,
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
        state_dict = triage_state.model_dump()
        if previous_state is not None:
            state_dict = validate_state_transition(previous_state, state_dict)
        state_dict = check_prank_exit(state_dict, previous_state)
        return True, state_dict

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
