import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select

from app.models.database import CallLogDB, AsyncSessionLocal
from app.services.ws_manager import ws_manager, _ALL_KEY
from app.api.routes.calls import worker_registry
import httpx

router = APIRouter(tags=["websocket"])
logger = logging.getLogger("ws_router")


async def _fetch_history_snapshot(session_id: Optional[str]) -> dict:
    """
    Returns a history_snapshot payload.

    - If session_id is provided: returns the single matching CallLog row (or an
      empty placeholder if not found yet).
    - If session_id is None: returns all existing CallLog rows.
    """
    async with AsyncSessionLocal() as db:
        if session_id:
            result = await db.execute(
                select(CallLogDB).where(CallLogDB.session_id == session_id)
            )
            row = result.scalar_one_or_none()
            if row:
                calls = [_row_to_dict(row)]
            else:
                calls = []
        else:
            result = await db.execute(select(CallLogDB))
            calls = [_row_to_dict(r) for r in result.scalars().all()]

    return {"calls": calls}


from app.services.incident_clustering import get_incident_state

def _row_to_dict(row: CallLogDB) -> dict:
    server_updated_at = None
    if row.incident_id:
        inc = get_incident_state(row.incident_id)
        if inc and inc.get("last_updated_at"):
            server_updated_at = inc["last_updated_at"].isoformat()
            
    return {
        "session_id": row.session_id,
        "incident_id": row.incident_id,
        "channel": row.channel,
        "language_detected": row.language_detected,
        "full_transcript": row.full_transcript,
        "triage_history": row.triage_history,
        "is_complete": row.is_complete,
        "dropped_at": row.dropped_at.isoformat() if row.dropped_at else None,
        "server_updated_at": server_updated_at
    }


@router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    session_id: Optional[str] = Query(default=None),
):
    """
    WebSocket endpoint for the live dashboard.

    Query params:
      ?session_id=<id>  — subscribe to one specific call session
      (omit)            — subscribe to ALL sessions (__all__)

    On connect:
      1. Immediately sends a "history_snapshot" with existing DB rows.
      2. Enters a receive loop waiting for client messages or disconnect.

    Supported incoming message types:
      - "operator_message": logged for now, no LLM injection yet.
    """
    subscribe_key = session_id if session_id else _ALL_KEY
    
    # Secure the dashboard websocket
    if not websocket.session.get("user"):
        logger.warning("Rejected unauthenticated websocket connection.")
        await websocket.close(code=1008) # Policy Violation
        return

    await ws_manager.connect(websocket, subscribe_key)
    logger.info(f"Dashboard WS opened, subscribe_key={subscribe_key!r}")

    try:
        # --- Step 1: send history snapshot before entering the loop ---
        snapshot = await _fetch_history_snapshot(session_id)
        await websocket.send_json(
            {
                "type": "history_snapshot",
                "session_id": session_id,
                "data": snapshot,
                "timestamp": __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .isoformat(),
            }
        )
        logger.info(f"Sent history_snapshot with {len(snapshot['calls'])} call(s)")

        # --- Step 2: receive loop ---
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "operator_message":
                target_session = msg.get('session_id')
                text = msg.get('text')
                logger.info(
                    f"Operator message received "
                    f"(session={target_session!r}): {text!r}"
                )
                
                # Forward to the specific worker subprocess via its registered IPC webhook
                worker_url = worker_registry.get(target_session)
                if worker_url:
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.post(
                                worker_url, 
                                json={"session_id": target_session, "text": text},
                                timeout=2.0
                            )
                            if resp.status_code == 200:
                                await websocket.send_json(
                                    {"type": "ack", "message": "Message sent to agent"}
                                )
                            else:
                                await websocket.send_json(
                                    {"type": "error", "message": f"Agent rejected message: {resp.status_code}"}
                                )
                    except Exception as e:
                        logger.error(f"Failed to relay operator message to worker {worker_url}: {e}")
                        await websocket.send_json(
                            {"type": "error", "message": "Failed to reach agent process"}
                        )
                else:
                    logger.warning(f"No worker registered for session_id={target_session!r}")
                    await websocket.send_json(
                        {"type": "error", "message": "Call not active or agent unreachable"}
                    )
            else:
                logger.warning(f"Unknown WS message type: {msg_type!r}")

    except WebSocketDisconnect:
        logger.info(f"Dashboard WS disconnected (subscribe_key={subscribe_key!r})")
    except Exception as exc:
        logger.error(f"Dashboard WS error: {exc}")
    finally:
        ws_manager.disconnect(websocket, subscribe_key)
