"""A file the user sent from the phone, already downloaded to the Mac."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    mime: str
    size: int = Field(ge=0)
    local_path: str
    sha256: str
