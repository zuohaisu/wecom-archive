from typing import Optional

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage
from app.db.session import get_db

app = FastAPI(title="365 WeCom Archive")

_MAX_LIMIT = 100


class MessageOut(BaseModel):
    msgid: str
    seq: int
    msgtype: Optional[str]
    sender: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    content_text: Optional[str]
    decrypt_status: str

    model_config = {"from_attributes": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/messages", response_model=list[MessageOut])
def get_messages(
    sender: Optional[str] = Query(None, description="Exact sender user ID"),
    q: Optional[str] = Query(None, description="Case-insensitive substring match on content_text"),
    msgtype: Optional[str] = Query(None, description="Exact message type (text, image, …)"),
    roomid: Optional[str] = Query(None, description="Exact room ID; omit for 1:1 messages"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    db: Session = Depends(get_db),
):
    query = db.query(ArchiveMessage)
    if sender:
        query = query.filter(ArchiveMessage.sender == sender)
    if q:
        query = query.filter(ArchiveMessage.content_text.ilike(f"%{q}%"))
    if msgtype:
        query = query.filter(ArchiveMessage.msgtype == msgtype)
    if roomid:
        query = query.filter(ArchiveMessage.roomid == roomid)
    return query.order_by(ArchiveMessage.msgtime.desc()).limit(limit).all()
