from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.app.core.auth import (
    AuthError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from backend.app.db.session import get_db
from backend.app.models import models as m

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


def _issue_tokens(user_id: str) -> TokenResponse:
    try:
        return TokenResponse(
            access_token=create_access_token(subject=user_id),
            refresh_token=create_refresh_token(subject=user_id),
        )
    except AuthError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    existing = db.query(m.User).filter_by(email=req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    if len(req.password) < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters")

    user = m.User(email=req.email, hashed_password=hash_password(req.password))
    db.add(user)
    db.flush()
    account = m.Account(user_id=user.id, name="default")
    db.add(account)

    return _issue_tokens(user.id)


@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(m.User).filter_by(email=form_data.username).first()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return _issue_tokens(user.id)


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Trades a still-valid refresh token for a brand-new access token AND
    a brand-new refresh token (a sliding window — see the module
    docstring in core/auth.py). Called automatically by the Android app's
    OkHttp Authenticator the moment any request comes back 401, never
    directly by the user, so this endpoint deliberately does NOT require
    the (already-expired) access token via the normal Authorization
    header — the refresh token in the body is its own proof of identity.
    """
    try:
        user_id = decode_token(req.refresh_token, expected_type="refresh")
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    user = db.query(m.User).filter_by(id=user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return _issue_tokens(user.id)


from fastapi.security import OAuth2PasswordBearer

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(_oauth2_scheme), db: Session = Depends(get_db)
) -> m.User:
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        user_id = decode_token(token, expected_type="access")
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    user = db.query(m.User).filter_by(id=user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_account_owner(
    account_id: str, user: m.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> m.Account:
    account = db.query(m.Account).filter_by(id=account_id, user_id=user.id).first()
    if account is None:
        raise HTTPException(status_code=403, detail="Not authorized for this account")
    return account
