import argparse
import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.models.database import OperatorDB, Base

DATABASE_URL = "sqlite+aiosqlite:///./triage.db"

def hash_password(password: str, salt: bytes = None) -> tuple[str, str]:
    if not salt:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return key.hex(), salt.hex()

async def create_or_update_operator(username: str, password: str):
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        result = await session.execute(select(OperatorDB).where(OperatorDB.username == username))
        operator = result.scalar_one_or_none()
        
        pwd_hash, salt_hex = hash_password(password)

        if operator:
            print(f"Operator '{username}' exists. Resetting password...")
            operator.password_hash = pwd_hash
            operator.salt = salt_hex
        else:
            print(f"Creating new operator '{username}'...")
            operator = OperatorDB(
                username=username,
                password_hash=pwd_hash,
                salt=salt_hex,
                created_at=datetime.now(timezone.utc)
            )
            session.add(operator)
        
        await session.commit()
        print(f"Success! Operator '{username}' is ready.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or reset an operator account.")
    parser.add_argument("username", help="Operator username")
    parser.add_argument("password", help="Operator password")
    args = parser.parse_args()
    
    asyncio.run(create_or_update_operator(args.username, args.password))
