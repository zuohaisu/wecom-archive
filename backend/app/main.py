from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient
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


class RecipientOut(BaseModel):
    receiver_userid: str
    receiver_type: Optional[str]

    model_config = {"from_attributes": True}


class MessageDetailOut(BaseModel):
    msgid: str
    seq: int
    msgtype: Optional[str]
    action: Optional[str]
    sender: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    content_text: Optional[str]
    decrypt_status: str
    decrypted_payload: Optional[dict]
    recipients: list[RecipientOut]

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


@app.get("/api/messages/{msgid}", response_model=MessageDetailOut)
def get_message(msgid: str, db: Session = Depends(get_db)):
    msg = db.query(ArchiveMessage).filter(ArchiveMessage.msgid == msgid).first()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    recipients = (
        db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == msg.id)
        .all()
    )

    return MessageDetailOut(
        msgid=msg.msgid,
        seq=msg.seq,
        msgtype=msg.msgtype,
        action=None,
        sender=msg.sender,
        roomid=msg.roomid,
        msgtime=msg.msgtime,
        content_text=msg.content_text,
        decrypt_status=msg.decrypt_status,
        decrypted_payload=msg.decrypted_payload,
        recipients=[RecipientOut.model_validate(r) for r in recipients],
    )
