import asyncio
from app.api.routes.websocket import _fetch_history_snapshot
async def main():
    res = await _fetch_history_snapshot(None)
    print(repr(res["calls"][0]["triage_history"]))
    print(type(res["calls"][0]["triage_history"]))
asyncio.run(main())
