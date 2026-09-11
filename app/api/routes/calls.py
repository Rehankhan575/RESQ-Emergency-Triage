from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import CallLogDB, AsyncSessionLocal
from app.services.ws_manager import ws_manager

router = APIRouter(prefix="/api/calls", tags=["calls"])

class BroadcastPayload(BaseModel):
    event_type: str
    data: dict

class RegisterWorkerPayload(BaseModel):
    worker_url: str

# In-memory registry mapping session_id -> worker IPC HTTP endpoint URL
worker_registry: dict[str, str] = {}


import os
from fastapi.responses import FileResponse
from fastapi import Request

# Dependency to get DB session
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@router.post("/{session_id}/recording_path")
async def update_recording_path(session_id: str, db: AsyncSession = Depends(get_db)):
    """Internal endpoint for worker to save recording path"""
    result = await db.execute(select(CallLogDB).where(CallLogDB.session_id == session_id))
    log = result.scalars().first()
    if not log:
        raise HTTPException(status_code=404, detail="Call not found")
        
    log.recording_path = f"recordings/{session_id}.wav"
    log.is_complete = True
    await db.commit()

    has_file = os.path.exists(log.recording_path)
    await ws_manager.broadcast(
        session_id,
        "recording_ready",
        {
            "session_id": session_id,
            "recording_path": log.recording_path,
            "has_recording": has_file,
            "is_complete": True
        }
    )
    return {"status": "ok", "recording_path": log.recording_path}

@router.get("/{session_id}/recording")
async def get_recording(session_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Secure endpoint to stream the recording WAV file."""
    if not request.session.get("user"):
        raise HTTPException(status_code=403, detail="Not authenticated")
        
    result = await db.execute(select(CallLogDB).where(CallLogDB.session_id == session_id))
    log = result.scalars().first()
    
    if not log or not log.recording_path:
        raise HTTPException(status_code=404, detail="Recording not found")
        
    if not os.path.exists(log.recording_path):
        raise HTTPException(status_code=404, detail="File missing from disk")
        
    return FileResponse(log.recording_path, media_type="audio/wav")

@router.post("/start")
async def start_call(db: AsyncSession = Depends(get_db)):
    session_id = str(uuid4())
    new_call = CallLogDB(
        session_id=session_id,
        language_detected="",
        full_transcript=[],
        triage_history=[],
        is_complete=False,
        dropped_at=None
    )
    db.add(new_call)
    await db.commit()
    return {"session_id": session_id, "status": "active"}

@router.get("/{session_id}/history")
async def get_history(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CallLogDB).where(CallLogDB.session_id == session_id))
    call_log = result.scalar_one_or_none()
    
    if not call_log:
        raise HTTPException(status_code=404, detail="Call log not found")
        
    return {
        "session_id": call_log.session_id,
        "language_detected": call_log.language_detected,
        "full_transcript": call_log.full_transcript,
        "triage_history": call_log.triage_history,
        "is_complete": call_log.is_complete,
        "dropped_at": call_log.dropped_at,
        "recording_path": call_log.recording_path,
        "has_recording": bool(call_log.recording_path and os.path.exists(call_log.recording_path))
    }

import asyncio
from datetime import datetime, timezone
from app.services.incident_clustering import assign_call_to_incident, persist_incident_state, get_incident_state
from app.api.routes.geocode import _cache as geocode_cache

def compute_urgency_score(severity: str, flag_for_human: bool) -> int:
    score = 0
    if severity == "CRITICAL":
        score += 100
    elif severity == "HIGH":
        score += 75
    elif severity == "MEDIUM":
        score += 50
    elif severity == "LOW":
        score += 25
    else:
        score += 60 # UNKNOWN / Null
    
    if flag_for_human:
        score += 30
        
    return score

@router.post("/{session_id}/broadcast")
async def broadcast_event(session_id: str, payload: BroadcastPayload, db: AsyncSession = Depends(get_db)):
    """Internal webhook for the LiveKit worker process to trigger WS broadcasts."""
    if payload.event_type == "triage_update":
        data = payload.data
        if data.get("emergency_type") and data.get("location"):
            # Synchronously check geocode cache for lat/lng (dashboard handles missing cache/frontend geocoding usually, but if backend wants to cluster, it needs it. Since Nominatim is called by frontend, it might NOT be in cache. Wait, the user prompt said "using the existing geocode.py internal cache logic". Let's fetch from cache, if missing, we skip clustering for this exact moment until it's cached, or we could fetch it async. Let's just use what's in cache.)
            loc_key = data["location"].strip().lower()
            geo = geocode_cache.get(loc_key)
            if not geo:
                from app.api.routes.geocode import geocode
                try:
                    geo = await geocode(location=data["location"])
                except Exception:
                    geo = None

            if geo:
                now = datetime.now(timezone.utc)
                incident_id = assign_call_to_incident(
                    session_id=session_id,
                    emergency_type=data["emergency_type"],
                    lat=geo["lat"],
                    lng=geo["lon"],
                    timestamp=now,
                    severity=data.get("severity")
                )
                payload.data["incident_id"] = incident_id
                asyncio.create_task(persist_incident_state(incident_id))
                
        # Compute Urgency Score
        incident_id = payload.data.get("incident_id")
        cluster_size = 1
        incident_severity = data.get("severity")
        # Ensure updated_at is always sent so frontend has the exact server time.
        # Fallback to now if somehow missing (though it should be set)
        server_updated_at = datetime.now(timezone.utc)

        if incident_id:
            inc_state = get_incident_state(incident_id)
            if inc_state:
                cluster_size = len(inc_state.get("members", []))
                incident_severity = inc_state.get("severity")
                server_updated_at = inc_state.get("last_updated_at", server_updated_at)
        
        base_urgency = compute_urgency_score(incident_severity, data.get("flag_for_human"))
        cluster_boost = min((cluster_size - 1) * 10, 30) if cluster_size > 1 else 0
        base_urgency += cluster_boost
        
        payload.data["urgency_base_score"] = base_urgency
        payload.data["cluster_size"] = cluster_size
        payload.data["server_updated_at"] = server_updated_at.isoformat()
        
        breakdown = f"{incident_severity or 'UNKNOWN'}"
        if data.get("flag_for_human"):
            breakdown += " + Escalation"
        if cluster_boost > 0:
            breakdown += f" + Cluster(x{cluster_size})"
        payload.data["urgency_breakdown_base"] = breakdown
        
        # Save triage update to database so it survives page reloads
        from sqlalchemy import select
        call_res = await db.execute(select(CallLogDB).where(CallLogDB.session_id == session_id))
        call_log = call_res.scalars().first()
        if not call_log:
            call_log = CallLogDB(
                session_id=session_id,
                channel=data.get("channel", "voice"),
                language_detected="",
                full_transcript=[],
                triage_history=[],
                is_complete=False,
                dropped_at=None
            )
            db.add(call_log)

        history = list(call_log.triage_history) if call_log.triage_history else []
        history.append(payload.data)
        call_log.triage_history = history
        if incident_id:
            call_log.incident_id = incident_id
        await db.commit()

    elif payload.event_type == "transcript_chunk":
        data = payload.data
        if data and data.get("text"):
            from sqlalchemy import select
            call_res = await db.execute(select(CallLogDB).where(CallLogDB.session_id == session_id))
            call_log = call_res.scalars().first()
            if not call_log:
                call_log = CallLogDB(
                    session_id=session_id,
                    channel="voice",
                    language_detected="",
                    full_transcript=[],
                    triage_history=[],
                    is_complete=False,
                    dropped_at=None
                )
                db.add(call_log)

            transcript_list = list(call_log.full_transcript) if call_log.full_transcript else []
            transcript_list.append({
                "speaker": data.get("speaker", "user"),
                "text": data.get("text", "")
            })
            call_log.full_transcript = transcript_list
            await db.commit()

    await ws_manager.broadcast(session_id, payload.event_type, payload.data)
    return {"status": "broadcasted"}

@router.post("/{session_id}/register_worker")
async def register_worker(session_id: str, payload: RegisterWorkerPayload):
    """Worker subprocess calls this to register its dynamic aiohttp URL."""
    worker_registry[session_id] = payload.worker_url
    return {"status": "registered"}
