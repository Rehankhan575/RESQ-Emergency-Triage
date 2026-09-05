from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
import asyncio

from app.models.database import CallLogDB, AsyncSessionLocal
from app.models.triage import EmergencyTypeEnum, TriageState
from app.api.routes.calls import BroadcastPayload, broadcast_event
from app.services.ws_manager import ws_manager

router = APIRouter(prefix="/api/silent-reports", tags=["silent_reports"])

class SilentReportForm(BaseModel):
    emergency_type: EmergencyTypeEnum
    location: str
    description: str
    people_affected: Optional[int] = None
    callback_number: Optional[str] = None

# Dependency to get DB session
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@router.post("")
async def submit_silent_report(form: SilentReportForm, db: AsyncSession = Depends(get_db)):
    session_id = str(uuid4())
    
    triage_state = TriageState(
        location=form.location,
        emergency_type=form.emergency_type,
        people_affected=form.people_affected,
        injuries=None,
        severity=None,
        confidence=1.0, # Maximum confidence for structured input
        flag_for_human=True, # Immediately escalate to operator
        next_question="",
        caller_stress_level=None,
        reasoning="Structured form submission — no AI inference performed, all fields directly reported by user."
    )
    
    # Prepend optional callback number to description if provided
    full_transcript = [f"SILENT FORM REPORT:\nDescription: {form.description}"]
    if form.callback_number:
        full_transcript.append(f"Callback Number: {form.callback_number}")
    
    new_call = CallLogDB(
        session_id=session_id,
        channel="silent_form",
        language_detected="unknown",
        full_transcript=full_transcript,
        triage_history=[triage_state.model_dump()],
        is_complete=True, # Silent reports are one-shots
        dropped_at=datetime.now(timezone.utc)
    )
    db.add(new_call)
    await db.commit()
    
    # Broadcast to dashboard via the exact same pipeline voice uses
    payload = BroadcastPayload(
        event_type="triage_update",
        data={
            "emergency_type": form.emergency_type.value,
            "location": form.location,
            "severity": None,
            "confidence": 1.0,
            "flag_for_human": True,
            "channel": "silent_form"
        }
    )
    
    # This also handles incident clustering synchronously because broadcast_event does it!
    # Wait, broadcast_event in calls.py expects a string session_id and BroadcastPayload
    await broadcast_event(session_id, payload, db=db)
    
    # Send a separate transcript update so the operator can read the description
    transcript_payload = {
        "text": full_transcript[0] + (f"\n{full_transcript[1]}" if len(full_transcript) > 1 else ""),
        "is_final": True
    }
    await ws_manager.broadcast(session_id, "transcript", transcript_payload)
    
    return {"session_id": session_id, "status": "submitted"}
