from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, Boolean, DateTime, JSON, Float

Base = declarative_base()

class OperatorDB(Base):
    __tablename__ = "operators"

    username = Column(String, primary_key=True, index=True)
    password_hash = Column(String)
    salt = Column(String)
    created_at = Column(DateTime)

class IncidentDB(Base):
    __tablename__ = "incidents"

    incident_id = Column(String, primary_key=True, index=True)
    emergency_type = Column(String, index=True)
    centroid_lat = Column(Float, nullable=True)
    centroid_lng = Column(Float, nullable=True)
    severity = Column(String)
    created_at = Column(DateTime)
    last_updated_at = Column(DateTime)
    members = Column(JSON, default=list)  # List of session_ids for easy retrieval

class CallLogDB(Base):
    __tablename__ = "call_logs"

    session_id = Column(String, primary_key=True, index=True)
    incident_id = Column(String, nullable=True, index=True)
    channel = Column(String, default="voice")
    language_detected = Column(String, default="en")
    full_transcript = Column(JSON, default=list)
    triage_history = Column(JSON, default=list)
    is_complete = Column(Boolean, default=False)
    dropped_at = Column(DateTime, nullable=True)
    reasoning = Column(String, nullable=True)

# Using aiosqlite for async SQLite
DATABASE_URL = "sqlite+aiosqlite:///./triage.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
