"""Who may talk to the relay: one person, in a 1:1 room. Everything else is ignored and logged."""

from __future__ import annotations

from dataclasses import dataclass

from agent_relay.core.registry import Registry
from agent_relay.log import get_logger

log = get_logger(__name__)

PERSON_KEY = "webex.person_id"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    leave_room: bool = False


class AccessControl:
    def __init__(self, registry: Registry, allowed_email: str, bot_person_id: str) -> None:
        self._registry = registry
        self._email = allowed_email.strip().lower()
        self._bot_id = bot_person_id

    async def pinned_person(self) -> str | None:
        return await self._registry.kv_get(PERSON_KEY)

    async def check_message(self, person_id: str, person_email: str, room_type: str) -> Verdict:
        if person_id == self._bot_id:
            return Verdict(False, "own message")
        if room_type != "direct":
            log.warning(f"ignored a message in a {room_type or 'unknown'} room; leaving it")
            return Verdict(False, "group room", leave_room=True)
        if not self._email:
            log.warning("ignored a message: webex.allowed_email is not configured")
            return Verdict(False, "allowed_email not configured")
        pinned = await self.pinned_person()
        if pinned is not None:
            if person_id != pinned:
                log.warning("ignored a message from an unexpected person id")
                return Verdict(False, "person id does not match the pinned id")
            return Verdict(True, "pinned")
        if person_email.strip().lower() != self._email:
            log.warning("ignored a message from an address other than allowed_email")
            return Verdict(False, "email not allowed")
        await self._registry.kv_set(PERSON_KEY, person_id)
        log.info("pinned the allowed person id on first contact")
        return Verdict(True, "first contact")

    async def check_action(self, person_id: str, room_type: str) -> Verdict:
        """Card submissions carry no email, so only the pinned person id is accepted."""
        if room_type != "direct":
            log.warning("ignored a card submission from a group room")
            return Verdict(False, "group room", leave_room=True)
        pinned = await self.pinned_person()
        if pinned is None or person_id != pinned:
            log.warning("ignored a card submission from someone other than the pinned person")
            return Verdict(False, "card submission from another person")
        return Verdict(True, "pinned")
