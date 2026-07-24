from typing import Optional

from pydantic import BaseModel


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
    recipients: list[RecipientOut]

    model_config = {"from_attributes": True}
