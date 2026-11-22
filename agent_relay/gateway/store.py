"""What the gateway remembers per server: how to sign in, the OAuth client, and the tokens.

One Keychain item per server, so a token never touches a file. The gateway key (what the apps send to
the gateway) lives with the MCP secrets, so `${keychain:gateway.key}` resolves in rendered configs.
"""

from __future__ import annotations

import json
import secrets

import keyring
import keyring.errors
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_relay.gateway.oauth import ClientInfo, Discovery, Tokens
from agent_relay.log import get_logger
from agent_relay.sync.secrets import MCP_KEYCHAIN_SERVICE

log = get_logger(__name__)

GATEWAY_SERVICE = "agent-relay-gateway"
KEY_ACCOUNT = "gateway.key"
KEY_REF = f"${{keychain:{KEY_ACCOUNT}}}"
KEY_HEADER = "X-Relay-Gateway-Key"


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovery: Discovery | None = None
    client: ClientInfo | None = None
    tokens: Tokens | None = None


class CredentialStore:
    """Credentials by server name. A locked or missing Keychain reads as signed out."""

    def load(self, name: str) -> Credentials:
        try:
            raw = keyring.get_password(GATEWAY_SERVICE, name)
        except keyring.errors.KeyringError:
            log.warning(f"the Keychain could not be read for MCP server {name}")
            return Credentials()
        if not raw:
            return Credentials()
        try:
            return Credentials.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            log.warning(f"the stored sign-in for MCP server {name} is unreadable; it is ignored")
            return Credentials()

    def save(self, name: str, credentials: Credentials) -> None:
        keyring.set_password(GATEWAY_SERVICE, name, credentials.model_dump_json())

    def sign_out(self, name: str) -> None:
        """Forget the tokens; the OAuth client is kept so the next sign-in needs no registration."""
        current = self.load(name)
        self.save(name, current.model_copy(update={"tokens": None}))

    def forget(self, name: str) -> None:
        try:
            keyring.delete_password(GATEWAY_SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            return


def gateway_key(create: bool = False) -> str | None:
    """The shared secret the apps send; made on first use when `create` is true."""
    try:
        found = keyring.get_password(MCP_KEYCHAIN_SERVICE, KEY_ACCOUNT)
        if found or not create:
            return found or None
        made = secrets.token_urlsafe(32)
        keyring.set_password(MCP_KEYCHAIN_SERVICE, KEY_ACCOUNT, made)
        return made
    except keyring.errors.KeyringError:
        return None
