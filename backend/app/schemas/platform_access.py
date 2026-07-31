"""Safe response schema for platform content-access request gates."""

from typing import Literal

from pydantic import BaseModel


class ContentAccessRequestOut(BaseModel):
    tenant_id: str
    recorded: Literal[True] = True
    granted: Literal[False] = False
    note: Literal[
        "access request recorded; this endpoint does not return message content"
    ] = "access request recorded; this endpoint does not return message content"
