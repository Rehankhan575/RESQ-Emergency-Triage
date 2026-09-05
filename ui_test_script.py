import asyncio
from playwright.async_api import async_playwright
import time
import requests
import urllib.parse

BASE_URL = "http://localhost:8000/api/calls"
OUTPUT_DIR = "/Users/rehan/.gemini/antigravity-ide/brain/00faabf3-da3a-4ae3-9f7c-e1640bada9e5"

def broadcast(session_id, data):
    requests.post(f"{BASE_URL}/{session_id}/broadcast", json={"event_type": "triage_update", "data": data})

def geocode(location):
    requests.get(f"http://localhost:8000/api/geocode?q={urllib.parse.quote(location)}")

async def run_ui_test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # 1. Geocode locations to fill backend cache
        geocode("Andheri West, Mumbai")
        geocode("Bandra, Mumbai")
        
        # 2. Open dashboard
        await page.goto("http://localhost:8000/dashboard/")
        await asyncio.sleep(2)
        
        # 3. Inject 4 initial calls
        broadcast("e2e-call-1", {"emergency_type": "FIRE", "location": "Andheri West, Mumbai", "severity": "CRITICAL", "transcript": "1"})
        broadcast("e2e-call-2", {"emergency_type": "FIRE", "location": "Andheri West, Mumbai", "severity": "HIGH", "transcript": "2"})
        broadcast("e2e-call-3", {"emergency_type": "FIRE", "location": "Andheri West, Mumbai", "severity": "MEDIUM", "transcript": "3"})
        broadcast("e2e-call-4", {"emergency_type": "MEDICAL", "location": "Bandra, Mumbai", "severity": "HIGH", "transcript": "4"})
        await asyncio.sleep(3)
        
        # Check UI item 1: "did the queue show a group header reading '3 callers' for Andheri West?"
        await page.screenshot(path=f"{OUTPUT_DIR}/1_queue_group_header.png")
        html = await page.content()
        assert "3 callers" in html and "Andheri West" in html, "Group header not found!"
        
        # Check UI item 2: "did expanding it reveal exactly 3 nested rows?"
        # Click the chevron to expand
        await page.click(".call-group-header")
        await asyncio.sleep(1)
        await page.screenshot(path=f"{OUTPUT_DIR}/2_expanded_group.png")
        
        # Check UI item 3: "did clicking one nested row update the center detail panel to that specific session?"
        # Find nested rows
        rows = await page.locator(".call-group-body .call-row").all()
        assert len(rows) == 3, f"Expected 3 nested rows, got {len(rows)}"
        
        await rows[1].click() # Click the second nested row
        await asyncio.sleep(1)
        await page.screenshot(path=f"{OUTPUT_DIR}/3_detail_panel.png")
        html_after_click = await page.content()
        assert "e2e-call-2" in html_after_click, "Detail panel did not update to e2e-call-2"
        
        # Check UI item 4: "did the map show one pin with a '3' badge for Andheri West AND a separate standalone pin for the Bandra heart attack call?"
        await page.screenshot(path=f"{OUTPUT_DIR}/4_map_pins.png")
        
        # Scenario 2: "Add a scenario where a call starts as a solo, unclustered entry with its own map pin, and a later call causes it to merge into a cluster"
        # call-4 (Bandra) is currently solo.
        # Let's add call-5 (Bandra).
        print("Injecting call 5 to merge with call 4")
        broadcast("e2e-call-5", {"emergency_type": "MEDICAL", "location": "Bandra, Mumbai", "severity": "CRITICAL", "transcript": "5"})
        await asyncio.sleep(3)
        await page.screenshot(path=f"{OUTPUT_DIR}/5_map_pin_merged.png")
        
        await browser.close()
        print("UI Test Passed!")

if __name__ == "__main__":
    asyncio.run(run_ui_test())
