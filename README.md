# SIH Triage

Voice Emergency Triage system.

## Project Structure

- `app/api/routes`: Contains the FastAPI endpoints and route handlers for the application.
- `app/core`: Core configuration, security, dependency injections, and application-level settings.
- `app/models`: Database models (SQLAlchemy) and data validation schemas (Pydantic).
- `app/agents/triage`: Contains the AI agent logic, specifically the LiveKit agents and LLM prompts for emergency triage.
- `app/services`: Core business logic, external API integrations, and utility services (e.g., interacting with OpenAI, Sarvam AI).
- `app/dashboard/static`: Static files (HTML, CSS, JS) and assets for the frontend monitoring dashboard.
