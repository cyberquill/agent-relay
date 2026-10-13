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


class Received(BaseModel):
    """What one phone message brought: the files saved, and the ones left out with the reason."""

    model_config = ConfigDict(frozen=True)

    attachments: tuple[Attachment, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.attachments and not self.skipped

    def merged(self, other: Received) -> Received:
        return Received(
            attachments=self.attachments + other.attachments, skipped=self.skipped + other.skipped
        )
