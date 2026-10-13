"""Schemas for `relay.toml` and `workspaces.toml`."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


BotName = Literal["control", "vscode", "cursor", "opencode"]
BOT_NAMES: tuple[BotName, ...] = get_args(BotName)
CONTROL: BotName = "control"
# Keychain accounts of other secrets, which no bot may claim (a test keeps this equal to `settings.Secret`).
OTHER_SECRET_ACCOUNTS = frozenset({"pwa-token", "opencode-password"})
CONTROL_ACCOUNT = "webex-bot-token"


class WebexConfig(_Section):
    allowed_email: str = ""
    bot_name: str = "agent-relay"
    api_base: str = "https://webexapis.com/v1"
    u2c_url: str = "https://u2c.wbx2.com/u2c/api/v1/catalog"
    # "two" splits `/new` into app + workspace + prompt, then model, if a phone mishandles ShowCard.
    new_card_layout: Literal["one", "two"] = "one"
    # One bot, so one 1:1 space, per app: bot name -> the Keychain account that holds its token.
    # Empty means the single bot (`webex-bot-token`) serves every app. `control` may be left out.
    bots: dict[BotName, str] = Field(default_factory=dict)

    @field_validator("bots")
    @classmethod
    def _accounts(cls, bots: dict[BotName, str]) -> dict[BotName, str]:
        seen: dict[str, BotName] = {}
        for bot, account in bots.items():
            if not account or account != account.strip() or len(account.split()) != 1:
                raise ValueError(f"webex.bots.{bot} must be a Keychain account name with no spaces")
            if account in OTHER_SECRET_ACCOUNTS or (account == CONTROL_ACCOUNT and bot != CONTROL):
                raise ValueError(f"webex.bots.{bot}: {account!r} holds another secret; pick its own name")
            if account in seen:
                raise ValueError(f"webex.bots.{bot} and {seen[account]} use the same Keychain account")
            seen[account] = bot
        return bots


READ_ONLY_TOOLS: tuple[str, ...] = (
    "read",
    "read_file",
    "view",
    "glob",
    "grep",
    "ls",
    "list",
    "list_dir",
    "search",
    "*_search",
    "search_*",
    "find_*",
    "get_*",
    "fetch",
    "webfetch",
    "think",
    "todo*",
    "read_*",
)


class ApprovalConfig(_Section):
    deadline_s: int = Field(default=600, ge=10)
    hook_timeout_margin_s: int = Field(default=60, ge=5)
    auto_allow: list[str] = Field(default_factory=lambda: list(READ_ONLY_TOOLS))
    # How long "allow this tool in this workspace" from the phone lasts.
    workspace_grant_s: int = Field(default=14_400, ge=60)

    @model_validator(mode="after")
    def _no_global_auto_approve(self) -> ApprovalConfig:
        for pattern in self.auto_allow:
            literals = sum(1 for c in pattern if c not in "*?[]! ")
            if literals < MIN_LITERAL_CHARS:
                raise ValueError(
                    f"approvals.auto_allow pattern {pattern!r} matches (nearly) every tool; "
                    "a global auto-approve is not allowed"
                )
        return self


MIN_LITERAL_CHARS = 2


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
    flush_bytes: int = 1_536
    flush_idle_s: float = 15.0
    max_message_bytes: int = 6_144
    pace_s: float = 2.0
    root_edit_budget: int = 8
    progress_edit_interval_s: float = Field(default=30.0, ge=5)
    quick_reply: bool = False


QueueMode = Literal["queue", "steer", "interrupt"]


class QueueConfig(_Section):
    default_mode: QueueMode = "queue"


class PresenceConfig(_Section):
    auto_away_on_lock: bool = True
    poll_s: float = Field(default=10.0, ge=1)


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
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    park: ParkConfig = Field(default_factory=ParkConfig)
    pwa: PwaConfig = Field(default_factory=PwaConfig)
    cdp: CdpConfig = Field(default_factory=CdpConfig)
    cursor_cli: CursorCliConfig = Field(default_factory=CursorCliConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    renderer: RendererConfig = Field(default_factory=RendererConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    ws: WsConfig = Field(default_factory=WsConfig)
    log_level: str = "INFO"
    debug_bodies: bool = False

    def approval_hook_timeout_s(self) -> int:
        return self.approvals.deadline_s + self.approvals.hook_timeout_margin_s

    def park_hook_timeout_s(self) -> int:
        return self.park.slice_s + self.park.hook_timeout_margin_s


class Workspace(_Section):
    name: str
    path: str
    # A multi-root `.code-workspace`; `path` is then its first folder, the cwd its hooks report.
    file: str | None = None


class WorkspacesConfig(_Section):
    workspaces: list[Workspace] = Field(default_factory=list)
