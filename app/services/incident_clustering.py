import math
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

from app.models.database import AsyncSessionLocal, IncidentDB, CallLogDB
from app.models.triage import SeverityEnum

logger = logging.getLogger("incident_clustering")

DISTANCE_THRESHOLD_KM = 0.5
TIME_WINDOW_MINUTES = 15

# Map severity string to an integer weight for max() calculation
SEVERITY_WEIGHT = {
    SeverityEnum.LOW: 1,
    SeverityEnum.MEDIUM: 2,
    SeverityEnum.HIGH: 3,
    SeverityEnum.CRITICAL: 4,
}

# In-memory registry for ultra-fast synchronous lookup
# Dict format: {
#   incident_id: {
#     "emergency_type": str,
#     "lat": float,
#     "lng": float,
#     "severity": str,
#     "last_updated_at": datetime,
#     "members": set[str]
#   }
# }
active_incidents: Dict[str, dict] = {}


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance between two points on the earth (specified in decimal degrees)."""
    # convert decimal degrees to radians 
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])

    # haversine formula 
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a)) 
    r = 6371 # Radius of earth in kilometers
    return c * r

def _max_severity(sev1: str, sev2: str) -> str:
    """Return the higher severity of the two."""
    if not sev1: return sev2
    if not sev2: return sev1
    w1 = SEVERITY_WEIGHT.get(sev1, 0)
    w2 = SEVERITY_WEIGHT.get(sev2, 0)
    return sev1 if w1 >= w2 else sev2

def assign_call_to_incident(session_id: str, emergency_type: str, lat: float, lng: float, timestamp: datetime, severity: str) -> str:
    """
    Synchronously assigns a call to an existing incident or creates a new one.
    Returns the incident_id.
    """
    best_incident_id = None

    for inc_id, inc_data in active_incidents.items():
        if inc_data["emergency_type"] != emergency_type:
            continue
        
        # Check time window
        time_diff = (timestamp - inc_data["last_updated_at"]).total_seconds() / 60.0
        if time_diff > TIME_WINDOW_MINUTES:
            continue
        
        # Check distance
        dist = _haversine_distance(lat, lng, inc_data["lat"], inc_data["lng"])
        if dist <= DISTANCE_THRESHOLD_KM:
            best_incident_id = inc_id
            break

    if best_incident_id:
        # Update existing incident
        inc_data = active_incidents[best_incident_id]
        inc_data["members"].add(session_id)
        inc_data["last_updated_at"] = timestamp
        if severity:
            inc_data["severity"] = _max_severity(inc_data["severity"], severity)
        logger.info(f"Clustering: Session {session_id} assigned to EXISTING incident {best_incident_id}")
        return best_incident_id
    else:
        # Create new incident
        new_inc_id = str(uuid.uuid4())
        active_incidents[new_inc_id] = {
            "emergency_type": emergency_type,
            "lat": lat,
            "lng": lng,
            "severity": severity,
            "created_at": timestamp,
            "last_updated_at": timestamp,
            "members": {session_id}
        }
        logger.info(f"Clustering: Session {session_id} started NEW incident {new_inc_id}")
        return new_inc_id

def get_incident_state(incident_id: str) -> Optional[dict]:
    """Returns the current state of an incident for urgency scoring."""
    return active_incidents.get(incident_id)

async def persist_incident_state(incident_id: str):
    """
    Asynchronously mirrors the in-memory incident state to the SQLite DB.
    """
    inc_data = active_incidents.get(incident_id)
    if not inc_data:
        return
    
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        result = await db.execute(select(IncidentDB).where(IncidentDB.incident_id == incident_id))
        incident = result.scalar_one_or_none()
        
        if not incident:
            incident = IncidentDB(
                incident_id=incident_id,
                emergency_type=inc_data["emergency_type"],
                centroid_lat=inc_data["lat"],
                centroid_lng=inc_data["lng"],
                created_at=inc_data["created_at"]
            )
            db.add(incident)
        
        incident.severity = inc_data["severity"]
        incident.last_updated_at = inc_data["last_updated_at"]
        incident.members = list(inc_data["members"])

        # Update CallLogDB
        for sid in inc_data["members"]:
            call_res = await db.execute(select(CallLogDB).where(CallLogDB.session_id == sid))
            call_row = call_res.scalar_one_or_none()
            if call_row:
                call_row.incident_id = incident_id
        
        await db.commit()
