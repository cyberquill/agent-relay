"""Schemas for `relay.toml` and `workspaces.toml`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Keys of earlier builds. They are accepted, ignored and reported (`settings.legacy_config_keys`), so a
# relay.toml written before the November round still loads; `relay config unset <key>` removes one.
LEGACY_KEYS: dict[str, tuple[str, ...]] = {
    "webex": ("bots", "bot_name", "new_card_layout"),
    "renderer": ("flush_bytes", "flush_idle_s", "progress_edit_interval_s", "quick_reply"),
}
LEGACY_SECTIONS: tuple[str, ...] = ("approvals", "presence")


def split_legacy(data: Mapping[str, object]) -> tuple[dict[str, object], list[str]]:
    """`data` without the legacy keys, and their dotted names (sorted) for the doctor."""
    found: list[str] = []
    clean: dict[str, object] = {}
    for key, value in data.items():
        legacy = LEGACY_KEYS.get(key)
        if key in LEGACY_SECTIONS:
            leaves = [f"{key}.{leaf}" for leaf in value] if isinstance(value, Mapping) else []
            found.extend(leaves or [key])
        elif legacy is not None and isinstance(value, Mapping):
            found.extend(f"{key}.{leaf}" for leaf in value if leaf in legacy)
            clean[key] = {leaf: item for leaf, item in value.items() if leaf not in legacy}
        else:
            clean[key] = value
    return clean, sorted(found)


class WebexConfig(_Section):
    allowed_email: str = ""
    api_base: str = "https://webexapis.com/v1"
    u2c_url: str = "https://u2c.wbx2.com/u2c/api/v1/catalog"


class QuestionsConfig(_Section):
    """How long a question to the phone (`ask_phone`, an app's own question) waits for the answer."""

    deadline_s: int = Field(default=600, ge=10)
    hook_timeout_margin_s: int = Field(default=60, ge=5)


class ParkConfig(_Section):
    deadline_s: int = Field(default=14_400, ge=60)
    slice_s: int = Field(default=1_500, ge=10)
    hook_timeout_margin_s: int = Field(default=120, ge=5)
    vscode_max_continuations: int = Field(default=20, ge=1)
    copilot_max_blocks: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _slice_within_deadline(self) -> ParkConfig:
        if self.slice_s > self.deadline_s:
            raise ValueError("park.slice_s must not exceed park.deadline_s")
        return self


class PwaConfig(_Section):
    enabled: bool = False
    port: int = 8787


class CdpConfig(_Section):
    enabled: bool = False
    port: int = 9223
    start_deadline_s: float = Field(default=20.0, ge=5)


class CursorCliConfig(_Section):
    idle_s: float = Field(default=900.0, ge=60)


class OpenCodeConfig(_Section):
    url: str = "http://127.0.0.1:4096"
    username: str = "opencode"


class AdaptersConfig(_Section):
    ahp: bool = True
    vscode: bool = True
    opencode: bool = True
    cursor_cli: bool = True


class RendererConfig(_Section):
    max_message_bytes: int = 6_144
    pace_s: float = 2.0
    root_edit_budget: int = 8
    # A long or rich answer goes out as `answer.html`: always, never, or (auto) when too big or rich.
    html_threshold: Literal["auto", "always", "never"] = "auto"


QueueMode = Literal["queue", "steer", "interrupt"]


class QueueConfig(_Section):
    default_mode: QueueMode = "queue"


class AttachmentsConfig(_Section):
    enabled: bool = True
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    # A files-only message waits this long for the text it belongs to.
    hold_s: int = Field(default=600, ge=10)
    inbox_dir: str = ".agent-relay/inbox"


class WsConfig(_Section):
    """Adding workspaces from the phone. Off until `roots` names at least one folder."""

    roots: list[str] = Field(default_factory=list)
    clone_hosts: list[str] = Field(default_factory=lambda: ["github.com"])
    clone_timeout_s: int = Field(default=300, ge=10)
    list_depth: int = Field(default=2, ge=1, le=4)


class RelayConfig(_Section):
    webex: WebexConfig = Field(default_factory=WebexConfig)
    questions: QuestionsConfig = Field(default_factory=QuestionsConfig)
    park: ParkConfig = Field(default_factory=ParkConfig)
    pwa: PwaConfig = Field(default_factory=PwaConfig)
    cdp: CdpConfig = Field(default_factory=CdpConfig)
    cursor_cli: CursorCliConfig = Field(default_factory=CursorCliConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    renderer: RendererConfig = Field(default_factory=RendererConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    ws: WsConfig = Field(default_factory=WsConfig)
    log_level: str = "INFO"
    debug_bodies: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy(cls, data: object) -> object:
        return split_legacy(data)[0] if isinstance(data, Mapping) else data

    def question_hook_timeout_s(self) -> int:
        return self.questions.deadline_s + self.questions.hook_timeout_margin_s

    def park_hook_timeout_s(self) -> int:
        return self.park.slice_s + self.park.hook_timeout_margin_s


class Workspace(_Section):
    name: str
    path: str
    # A multi-root `.code-workspace`; `path` is then its first folder, the cwd its hooks report.
    file: str | None = None


class WorkspacesConfig(_Section):
    workspaces: list[Workspace] = Field(default_factory=list)
