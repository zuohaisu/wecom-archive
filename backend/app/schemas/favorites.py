"""Stable HTTP contracts for tenant-shared favorites (RND-366)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

FavoriteObjectType = Literal["message", "media"]
FavoriteSourcePage = Literal["messages", "media", "favorites"]
FavoriteAction = Literal["favorite", "unfavorite"]
FavoriteMutationResult = Literal[
    "favorited",
    "already_favorited",
    "unfavorited",
    "already_unfavorited",
    "not_found",
]


class _FavoriteRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FavoriteObjectIn(_FavoriteRequestModel):
    """Canonical target identity: archive msgid or tenant MediaFile.id."""

    object_type: FavoriteObjectType
    object_id: str = Field(min_length=1, max_length=64)
    source_page: Optional[FavoriteSourcePage] = None

    @model_validator(mode="after")
    def validate_object_id(self) -> "FavoriteObjectIn":
        if self.object_id != self.object_id.strip() or any(
            ord(character) < 32 for character in self.object_id
        ):
            raise ValueError("object_id must be a trimmed, printable identifier")
        if self.object_type == "media":
            if not self.object_id.isascii() or not self.object_id.isdigit():
                raise ValueError("media object_id must be a positive MediaFile id")
            media_id = int(self.object_id)
            if media_id < 1 or media_id > 2_147_483_647:
                raise ValueError("media object_id is outside the supported range")
        return self


class FavoriteMutationIn(FavoriteObjectIn):
    pass


class FavoriteBatchIn(_FavoriteRequestModel):
    action: FavoriteAction
    items: list[FavoriteObjectIn] = Field(min_length=1, max_length=100)


class FavoriteStatusIn(_FavoriteRequestModel):
    items: list[FavoriteObjectIn] = Field(min_length=1, max_length=100)


class FavoriteMutationItemOut(BaseModel):
    object_type: FavoriteObjectType
    object_id: str
    result: FavoriteMutationResult
    duplicate: bool = False


class FavoriteMutationOut(BaseModel):
    requested: int
    unique: int
    applied: int
    unchanged: int
    not_found: int
    items: list[FavoriteMutationItemOut]


class FavoriteStatusItemOut(BaseModel):
    object_type: FavoriteObjectType
    object_id: str
    result: Literal["found", "not_found"]
    is_favorited: Optional[bool] = None
    duplicate: bool = False


class FavoriteStatusOut(BaseModel):
    requested: int
    unique: int
    items: list[FavoriteStatusItemOut]


class FavoriteItemOut(BaseModel):
    favorite_id: str
    object_type: FavoriteObjectType
    object_id: str
    favorited_by_admin_user_id: Optional[str]
    favorited_at: datetime
    source_page: Optional[FavoriteSourcePage] = None
    message_time_ms: Optional[int] = None
    conversation_id: Optional[str] = None
    conversation_type: Optional[Literal["direct", "group"]] = None
    staff_id: Optional[str] = None
    contact_id: Optional[str] = None
    sender_id: Optional[str] = None
    message_type: Optional[str] = None
    preview: Optional[str] = None
    media_file_id: Optional[int] = None
    media_type: Optional[str] = None
    media_mime_type: Optional[str] = None
    media_size_bytes: Optional[int] = None


class FavoritePageOut(BaseModel):
    items: list[FavoriteItemOut]
    total: int
    limit: int
    offset: int
    has_more: bool
