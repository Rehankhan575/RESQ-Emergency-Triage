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

MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-3.6-flash",
]
MODEL_NAME = MODEL_CANDIDATES[0]

# Per-attempt hard ceiling — short enough that the fallback loop can exhaust
# MODEL_CANDIDATES within the outer wait_for() budget in voice_agent.py (8s).
_PER_ATTEMPT_TIMEOUT_S = 3.5


async def warm_up_gemini_connection() -> None:
    """
    Fire a trivial no-op request on the module-level `client` singleton so the
    underlying TLS/HTTP connection is established and pooled BEFORE any real
    caller arrives.  This is pure best-effort: a failure must never crash the
    worker or block it from accepting calls.
    """
    import time as _time
    t0 = _time.perf_counter()
    for model_name in MODEL_CANDIDATES:
        try:
            chat = client.aio.chats.create(
                model=model_name,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=4,
                ),
            )
            resp = await chat.send_message("ping")
            elapsed = _time.perf_counter() - t0
            logger.info(f"Gemini connection warmed in {elapsed:.2f}s on model={model_name} (response: {str(resp.text)[:20]!r})")
            return
        except Exception as exc:
            logger.warning(f"Gemini warm-up failed for model={model_name}: {exc}")
    elapsed = _time.perf_counter() - t0
    logger.warning(f"Gemini warm-up failed for all candidates after {elapsed:.2f}s")


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

7. Prank / False Alarm Extraction:
   - Set `is_prank` = True ONLY when the caller EXPLICITLY and unambiguously states this is a joke/prank/test call (e.g. "ye prank hai", "just joking", "test call tha", "maine mazak kiya"). NEVER infer `is_prank` from ambiguous, sarcastic, or casual language alone. Otherwise, set `is_prank` = False.
   - If `is_prank` is True AND no emergency signals (`emergency_type` / `injuries` / `severity`) were reported, set `next_question` to: "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye." and `flag_for_human` = False.
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
       "is_prank": boolean or null
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

    # Rule 1b: deterministic enforcement of Rule 5 escalation criteria
    # (LLM is prompt-instructed to do this but doesn't always comply — enforce in code)
    cur_sev_raw = _get_state_attr(guarded, "severity", None)
    cur_sev_str = cur_sev_raw.value if hasattr(cur_sev_raw, "value") else cur_sev_raw
    cur_injuries = _get_state_attr(guarded, "injuries", None)
    current_flag = bool(_get_state_attr(guarded, "flag_for_human", False))
    must_escalate = (
        cur_sev_str == "CRITICAL"
        or (cur_sev_str == "HIGH" and cur_injuries is True)
    )
    if must_escalate and not current_flag:
        logger.warning(
            f"validate_state_transition override: forcing flag_for_human=True "
            f"(severity={cur_sev_str}, injuries={cur_injuries}, llm_output=False)"
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


PRANK_CLOSING_LINE = (
    "Theek hai, dhanyavaad. Agar kisi ko sach mein madad chahiye ho toh phir se call kijiye."
)


def _field_is_set(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str) and val.strip().lower() in ("none", "null", ""):
        return False
    return True


def check_prank_exit(
    previous_state: Any,
    new_state: Any,
) -> Any:
    """
    Evaluates early-exit safety for prank/false-alarm calls.

    Case A (pure prank): is_prank is True and emergency_type/injuries/severity are
    null across BOTH previous_state and new_state -> close the call.
    Case B (prank after real distress): is_prank is True but previous_state already
    had a non-null emergency_type, injuries, or severity -> do not early-exit;
    keep is_prank as metadata and force flag_for_human = True.
    """
    if new_state is None:
        return new_state

    if not bool(_get_state_attr(new_state, "is_prank", False)):
        return new_state

    is_dict = isinstance(new_state, dict)
    guarded = dict(new_state) if is_dict else new_state.model_copy()

    prev_etype = _get_state_attr(previous_state, "emergency_type") if previous_state else None
    prev_inj = _get_state_attr(previous_state, "injuries") if previous_state else None
    prev_sev = _get_state_attr(previous_state, "severity") if previous_state else None
    prev_has_signal = _field_is_set(prev_etype) or _field_is_set(prev_inj) or _field_is_set(prev_sev)

    curr_etype = _get_state_attr(guarded, "emergency_type")
    curr_inj = _get_state_attr(guarded, "injuries")
    curr_sev = _get_state_attr(guarded, "severity")
    curr_has_signal = _field_is_set(curr_etype) or _field_is_set(curr_inj) or _field_is_set(curr_sev)

    if not prev_has_signal and not curr_has_signal:
        if is_dict:
            guarded["next_question"] = PRANK_CLOSING_LINE
            guarded["flag_for_human"] = False
        else:
            guarded.next_question = PRANK_CLOSING_LINE
            guarded.flag_for_human = False

        logger.info(
            "check_prank_exit: Case A (pure prank) — early exit. "
            f"[previous emergency_type/injuries/severity={prev_etype}/{prev_inj}/{prev_sev}, "
            f"llm_output emergency_type/injuries/severity={curr_etype}/{curr_inj}/{curr_sev}, "
            f"enforced next_question=closing line, flag_for_human=False]"
        )
        return guarded

    if is_dict:
        guarded["is_prank"] = True
        guarded["flag_for_human"] = True
        curr_next_q = guarded.get("next_question") or ""
        if PRANK_CLOSING_LINE in curr_next_q or "phir se call kijiye" in curr_next_q.lower():
            guarded["next_question"] = (
                "Aapne pehle emergency report ki thi. Kripya line par bane rahein, "
                "human operator se connect kiya ja raha hai."
            )
    else:
        guarded.is_prank = True
        guarded.flag_for_human = True
        curr_next_q = guarded.next_question or ""
        if PRANK_CLOSING_LINE in curr_next_q or "phir se call kijiye" in curr_next_q.lower():
            guarded.next_question = (
                "Aapne pehle emergency report ki thi. Kripya line par bane rahein, "
                "human operator se connect kiya ja raha hai."
            )

    trigger = []
    if _field_is_set(prev_etype) or _field_is_set(curr_etype):
        trigger.append("emergency_type")
    if _field_is_set(prev_inj) or _field_is_set(curr_inj):
        trigger.append("injuries")
    if _field_is_set(prev_sev) or _field_is_set(curr_sev):
        trigger.append("severity")

    logger.warning(
        "check_prank_exit: Case B (prank after real distress) — early exit blocked. "
        f"[triggered_by={trigger}, previous emergency_type/injuries/severity="
        f"{prev_etype}/{prev_inj}/{prev_sev}, llm_output emergency_type/injuries/severity="
        f"{curr_etype}/{curr_inj}/{curr_sev}, enforced is_prank=True, flag_for_human=True]"
    )
    return guarded


_RETRYABLE_CODES = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")

async def process_turn(
    user_transcript: str,
    history_before_turn: List[str],
    previous_state: Optional[Any] = None,
    _max_retries_per_model: int = 2,
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
    # Build Gemini-format history from the pre-turn snapshot
    gemini_history = []
    for i, text in enumerate(history_before_turn):
        role = "user" if i % 2 == 0 else "model"
        gemini_history.append({"role": role, "parts": [{"text": text}]})

    logger.info(f"Gemini History Context: {gemini_history}")

    for model_name in MODEL_CANDIDATES:
        for attempt in range(1, _max_retries_per_model + 1):
            try:
                chat = client.aio.chats.create(
                    model=model_name,
                    history=gemini_history,
                    config=types.GenerateContentConfig(
                        system_instruction=TriagePrompt.get_system_prompt(),
                        temperature=0.3,
                        response_mime_type="application/json",
                        response_schema=TriageState,
                    ),
                )

                response = await asyncio.wait_for(
                    chat.send_message(user_transcript), timeout=_PER_ATTEMPT_TIMEOUT_S
                )
                raw_text = response.text.strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text[7:]
                elif raw_text.startswith("```"):
                    raw_text = raw_text[3:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3]

                parsed_json = json.loads(raw_text.strip())

                # Validate against TriageState Pydantic model
                triage_state = TriageState(**parsed_json)
                state_dict = triage_state.model_dump()
                if previous_state is not None:
                    state_dict = validate_state_transition(previous_state, state_dict)
                state_dict = check_prank_exit(previous_state, state_dict)
                logger.info(f"LLM succeeded on model={model_name}")
                return True, state_dict

            except asyncio.TimeoutError:
                # Model hung — don't retry the same model, fall through to next candidate.
                logger.warning(
                    f"Per-attempt timeout ({_PER_ATTEMPT_TIMEOUT_S}s) on model={model_name} "
                    f"(attempt {attempt}/{_max_retries_per_model}) — moving to next model"
                )
                break

            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Validation or Parsing Error on model={model_name}: {e}")
                break

            except Exception as e:
                err_str = str(e)
                is_retryable = any(code in err_str for code in _RETRYABLE_CODES)
                if is_retryable:
                    logger.warning(
                        f"Retryable API error on model={model_name}, moving directly to next model: {err_str[:120]}"
                    )
                    break
                logger.warning(f"API Error on model={model_name}: {e}")
                break

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
        print(f"=== Triage Agent Test — Candidates: {MODEL_CANDIDATES} ===\n")
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

