import os
import hashlib
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, OperatorDB

router = APIRouter(tags=["auth"])

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def verify_password(plain_password: str, salt_hex: str, hashed_password: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt, 100000)
        return key.hex() == hashed_password
    except Exception:
        return False

@router.get("/login")
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/dashboard/", status_code=303)
    
    _DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dashboard", "static")
    return FileResponse(os.path.join(_DASHBOARD_DIR, "login.html"))

@router.post("/login")
async def login_post(
    request: Request, 
    username: str = Form(...), 
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(OperatorDB).where(OperatorDB.username == username))
    operator = result.scalar_one_or_none()
    
    if operator and verify_password(password, operator.salt, operator.password_hash):
        request.session["user"] = username
        return RedirectResponse(url="/dashboard/", status_code=303)
    
    # Generic error message to prevent enumeration
    _DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dashboard", "static")
    with open(os.path.join(_DASHBOARD_DIR, "login.html"), "r") as f:
        html = f.read()
    
    # Inject an error message roughly into the HTML
    error_html = '<div id="error" style="color: #ff6b6b; margin-bottom: 1rem; text-align: center;">Invalid username or password</div>'
    html = html.replace('<!-- ERROR_PLACEHOLDER -->', error_html)
    return HTMLResponse(content=html)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
