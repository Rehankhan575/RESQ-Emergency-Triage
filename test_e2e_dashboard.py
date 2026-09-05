import requests
import time
from datetime import datetime

BASE_URL = "http://localhost:8000/api/calls"

def broadcast(session_id, data):
    payload = {
        "event_type": "triage_update",
        "data": data
    }
    resp = requests.post(f"{BASE_URL}/{session_id}/broadcast", json=payload)
    print(f"[{session_id}] Status {resp.status_code}: {resp.text}")

def run_test():
    print("--- Starting E2E Dashboard Clustering Test ---")
    
    # 1. Call 1 - Fire (CRITICAL)
    print("\nInjecting Call 1: Fire in Andheri West (CRITICAL)")
    broadcast("e2e-call-1", {
        "emergency_type": "FIRE",
        "location": "Andheri West, Mumbai",
        "people_affected": "10",
        "injuries": "Unknown",
        "severity": "CRITICAL",
        "caller_stress_level": "High",
        "confidence": 0.95,
        "flag_for_human": False,
        "next_question": "Are you safe right now?"
    })
    time.sleep(1)

    # 2. Call 2 - Fire (HIGH) - Same location cluster
    print("\nInjecting Call 2: Fire in Andheri West (HIGH)")
    broadcast("e2e-call-2", {
        "emergency_type": "FIRE",
        "location": "Andheri West, Mumbai",
        "people_affected": "15",
        "injuries": "2",
        "severity": "HIGH",
        "caller_stress_level": "Medium",
        "confidence": 0.88,
        "flag_for_human": False,
        "next_question": "Can you see the flames?"
    })
    time.sleep(1)

    # 3. Call 3 - Fire (MEDIUM) - Same location cluster
    print("\nInjecting Call 3: Fire in Andheri West (MEDIUM)")
    broadcast("e2e-call-3", {
        "emergency_type": "FIRE",
        "location": "Andheri West, Mumbai",
        "people_affected": "5",
        "injuries": "None",
        "severity": "MEDIUM",
        "caller_stress_level": "Low",
        "confidence": 0.82,
        "flag_for_human": False,
        "next_question": "Is the fire spreading?"
    })
    time.sleep(1)

    # 4. Call 4 - Medical (HIGH) - Completely different incident
    print("\nInjecting Call 4: Heart Attack in Bandra (HIGH)")
    broadcast("e2e-call-4", {
        "emergency_type": "MEDICAL",
        "location": "Bandra, Mumbai",
        "people_affected": "1",
        "injuries": "Unconscious",
        "severity": "HIGH",
        "caller_stress_level": "High",
        "confidence": 0.99,
        "flag_for_human": True,
        "next_question": "Is the patient breathing?"
    })
    time.sleep(1)

    print("\n--- Test Complete ---")
    print("Check the dashboard UI: You should see 1 group header for 'Andheri West' with 3 callers (CRITICAL red color),")
    print("and 1 separate row for 'Bandra' (HIGH orange color).")

if __name__ == "__main__":
    run_test()
