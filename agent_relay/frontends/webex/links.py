"""Make links in agent output inert, so Webex builds no preview that could carry data out.

The logic lives in `agent_relay.frontends.text`, shared with the HTML renderer; this name stays for
the Webex renderer's imports.
"""

from __future__ import annotations

from agent_relay.frontends.text import neutralise_links

__all__ = ["neutralise_links"]
