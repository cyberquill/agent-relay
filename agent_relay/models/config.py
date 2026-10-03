"""Schemas for `relay.toml` and `workspaces.toml`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebexConfig(_Section):
    allowed_email: str = ""
    bot_name: str = "agent-relay"
    api_base: str = "https://webexapis.com/v1"
    u2c_url: str = "https://u2c.wbx2.com/u2c/api/v1/catalog"


class ApprovalConfig(_Section):
    deadline_s: int = Field(default=600, ge=10)
    hook_timeout_margin_s: int = Field(default=60, ge=5)


class ParkConfig(_Section):
    deadline_s: int = Field(default=14_400, ge=60)
    slice_s: int = Field(default=1_500, ge=10)
    hook_timeout_margin_s: int = Field(default=120, ge=5)
    local_max_continuations: int = Field(default=20, ge=1)
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


class OpenCodeConfig(_Section):
    url: str = "http://127.0.0.1:4096"
    username: str = "opencode"


class AdaptersConfig(_Section):
    ahp: bool = True
    vscode_local: bool = True
    opencode: bool = True
    cursor_cli: bool = True


class RendererConfig(_Section):
    flush_bytes: int = 1_536
    flush_idle_s: float = 15.0
    max_message_bytes: int = 6_144
    pace_s: float = 2.0
    root_edit_budget: int = 8


class RelayConfig(_Section):
    webex: WebexConfig = Field(default_factory=WebexConfig)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    park: ParkConfig = Field(default_factory=ParkConfig)
    pwa: PwaConfig = Field(default_factory=PwaConfig)
    cdp: CdpConfig = Field(default_factory=CdpConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    renderer: RendererConfig = Field(default_factory=RendererConfig)
    log_level: str = "INFO"
    debug_bodies: bool = False

    def approval_hook_timeout_s(self) -> int:
        return self.approvals.deadline_s + self.approvals.hook_timeout_margin_s

    def park_hook_timeout_s(self) -> int:
        return self.park.slice_s + self.park.hook_timeout_margin_s


class Workspace(_Section):
    name: str
    path: str


class WorkspacesConfig(_Section):
    workspaces: list[Workspace] = Field(default_factory=list)
