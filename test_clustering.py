import asyncio
import logging
from datetime import datetime, timezone, timedelta

# Setup basic logging to see the output
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("test")

# Import the clustering service
from app.services.incident_clustering import assign_call_to_incident, active_incidents

def run_test():
    logger.info("--- Starting Incident Clustering Test ---")
    
    # Base timestamp
    t0 = datetime.now(timezone.utc)
    
    # 1. Inject Call 1: FIRE at lat=19.1136, lng=72.8406 at T=0
    logger.info("Injecting Call 1: FIRE at lat=19.1136, lng=72.8406, T=0")
    inc_1 = assign_call_to_incident(
        session_id="call-1",
        emergency_type="FIRE",
        lat=19.1136,
        lng=72.8406,
        timestamp=t0,
        severity="HIGH"
    )
    logger.info(f"Result: {inc_1}")
    
    # 2. Inject Call 2: FIRE at lat=19.1160, lng=72.8420 (~300m away) at T=2m
    logger.info("\nInjecting Call 2: FIRE at lat=19.1160, lng=72.8420, T=2m")
    inc_2 = assign_call_to_incident(
        session_id="call-2",
        emergency_type="FIRE",
        lat=19.1160,
        lng=72.8420,
        timestamp=t0 + timedelta(minutes=2),
        severity="CRITICAL"
    )
    logger.info(f"Result: {inc_2}")
    assert inc_1 == inc_2, "Call 1 and Call 2 should merge!"
    assert active_incidents[inc_1]["severity"] == "CRITICAL", "Severity should be maxed to CRITICAL"
    
    # 3. Inject Call 3: MEDICAL at lat=19.1136, lng=72.8406 at T=3m
    logger.info("\nInjecting Call 3: MEDICAL at lat=19.1136, lng=72.8406, T=3m")
    inc_3 = assign_call_to_incident(
        session_id="call-3",
        emergency_type="MEDICAL",
        lat=19.1136,
        lng=72.8406,
        timestamp=t0 + timedelta(minutes=3),
        severity="MEDIUM"
    )
    logger.info(f"Result: {inc_3}")
    assert inc_3 != inc_1, "Call 3 should NOT merge (different type)"
    
    # 4. Inject Call 4: FIRE at lat=19.1136, lng=72.8406 at T=20m (Clearly outside time window)
    logger.info("\nInjecting Call 4: FIRE at lat=19.1136, lng=72.8406, T=20m")
    inc_4 = assign_call_to_incident(
        session_id="call-4",
        emergency_type="FIRE",
        lat=19.1136,
        lng=72.8406,
        timestamp=t0 + timedelta(minutes=20),
        severity="HIGH"
    )
    logger.info(f"Result: {inc_4}")
    assert inc_4 != inc_1, "Call 4 should NOT merge (outside time window)"

    # 5. Inject Call 5: FIRE at T=4m, but distance is ~520m away (lat=19.1180, lng=72.8425)
    # 1 degree lat is ~111km, so 0.0044 lat diff is ~488m. Let's use 19.1182 to be safely ~520m
    logger.info("\nInjecting Call 5: FIRE at lat=19.1182, lng=72.8425 (Boundary check ~530m away) at T=4m")
    inc_5 = assign_call_to_incident(
        session_id="call-5",
        emergency_type="FIRE",
        lat=19.1182,
        lng=72.8425,
        timestamp=t0 + timedelta(minutes=4),
        severity="MEDIUM"
    )
    logger.info(f"Result: {inc_5}")
    assert inc_5 != inc_1, "Call 5 should NOT merge (just outside 500m distance threshold)"

    # 6. Inject Call 6: FIRE at lat=19.1136, lng=72.8406 at T=15m 30s (Boundary check time)
    # The most recent update to inc_1 was at T=2m (from Call 2).
    # So 15 minutes after T=2m is T=17m.
    # To be just outside the 15m window from the LAST update, we need T=17m 30s.
    logger.info("\nInjecting Call 6: FIRE at lat=19.1136, lng=72.8406 (Boundary check time window) at T=17m 30s")
    inc_6 = assign_call_to_incident(
        session_id="call-6",
        emergency_type="FIRE",
        lat=19.1136,
        lng=72.8406,
        timestamp=t0 + timedelta(minutes=17, seconds=30),
        severity="LOW"
    )
    logger.info(f"Result: {inc_6}")
    assert inc_6 != inc_1, "Call 6 should NOT merge (just outside 15m rolling time threshold)"

    logger.info("\n--- Test Passed! ---")
    logger.info(f"Total active incidents: {len(active_incidents)}")
    for inc_id, data in active_incidents.items():
        logger.info(f"Incident {inc_id}: {data['emergency_type']} | severity={data['severity']} | members={data['members']}")

if __name__ == "__main__":
    run_test()
