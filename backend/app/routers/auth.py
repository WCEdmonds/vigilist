from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.schemas import LoginRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])

serializer = URLSafeTimedSerializer(settings.secret_key)
SESSION_COOKIE = "descubre_session"
MAX_AGE = 60 * 60 * 24 * 7  # 1 week


def get_current_user(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        username = serializer.loads(token, max_age=MAX_AGE)
    except Exception:
        raise HTTPException(status_code=401, detail="Session expired")
    return username


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    if body.username != settings.auth_username or body.password != settings.auth_password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = serializer.dumps(body.username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"username": body.username}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(username: str = Depends(get_current_user)):
    return {"username": username}
