from enum import Enum
from typing import Literal, Optional, List
from pydantic import BaseModel
from datetime import datetime

class SeverityEnum(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class EmergencyTypeEnum(str, Enum):
    FIRE = "FIRE"
    MEDICAL = "MEDICAL"
    ACCIDENT = "ACCIDENT"
    FLOOD = "FLOOD"
    CRIME = "CRIME"
    OTHER = "OTHER"

class TriageState(BaseModel):
    location: Optional[str] = None
    emergency_type: Optional[EmergencyTypeEnum] = None
    people_affected: Optional[int] = None
    injuries: Optional[bool] = None
    severity: Optional[SeverityEnum] = None
    confidence: float
    flag_for_human: bool
    next_question: str
    caller_stress_level: Optional[Literal['calm', 'anxious', 'panicking']] = None
    reasoning: Optional[str] = None
    is_prank: Optional[bool] = None

class CallLog(BaseModel):
    session_id: str
    channel: str = "voice"
    language_detected: str
    full_transcript: List[str]
    triage_history: List[TriageState]
    is_complete: bool
    dropped_at: Optional[datetime] = None
