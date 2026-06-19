"""Web cockpit — a flicker-free FastAPI + JS UI over the existing Python engine.

The whole engine (feeds / analysis / agent / execution / journal) is reused
unchanged; this layer only serialises it to JSON and serves a static single-page
cockpit that polls and updates in place (no full-page redraw, no fade). Run with:

    uvicorn web.server:app --reload      # then open http://localhost:8000

Streamlit (dashboard/app.py) stays as a fallback.
"""
