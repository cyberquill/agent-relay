"""A file the user sent from the phone, already downloaded to the Mac."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)


def takes_inline(mime: str) -> bool:
    """Whether a coding agent can read a file of this type as part of the prompt.

    Images and PDFs only. Anything else (text, CSV, zip, HEIC, office files) goes as a path the agent
    reads with its own tools: a model that does not take the type fails that turn and every later one
    (the file stays in the history), and a HEIC makes OpenCode stay busy until it is stopped.
    """
    return mime.split(";", 1)[0].strip().lower() in INLINE_TYPES


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
