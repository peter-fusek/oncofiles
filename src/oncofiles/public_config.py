"""Public site configuration for the landing page (``GET /api/public-config``).

The landing offers the trial paths that are really open right now: the
"sign in with Google and pick your Drive folder" path only when onboarding is
``open`` (#852), and the "send your first e-mail with an attachment" path only
when the inbound welcome address can actually receive mail (#950).

Rules (the same as ``/health``): no authentication, no DB call, no network,
no blocking I/O — runtime env only. It never says WHICH inbound variable is
missing, only whether all of them are set, and never returns a value.
"""

from __future__ import annotations

import os

from oncofiles import onboarding_gate
from oncofiles.inbound_email import INBOUND_DOMAIN

WELCOME_ADDRESS = f"welcome@{INBOUND_DOMAIN}"

# Every one of these must be set before the landing may show the welcome
# address: the Brevo route token, the durable queue key (#952), the holding
# store key and the authserv-id of the trusted Authentication-Results line.
INBOUND_WELCOME_ENVS = (
    "INBOUND_BREVO_TOKEN",
    "INBOUND_JOBS_SECRET",
    "HOLDING_STORE_SECRET",
    "INBOUND_BREVO_AUTHSERV_ID",
)

# Short browser/proxy cache: a mode flip shows up on the landing within a minute.
CACHE_SECONDS = 60


def inbound_welcome_enabled() -> bool:
    """True only when every variable of ``INBOUND_WELCOME_ENVS`` is set."""
    return all(os.environ.get(name, "").strip() for name in INBOUND_WELCOME_ENVS)


def public_config() -> dict:
    """The landing's public flags; env only, never the DB."""
    return {
        "inbound_welcome_enabled": inbound_welcome_enabled(),
        "onboarding_mode": onboarding_gate.onboarding_mode(),
        "welcome_address": WELCOME_ADDRESS,
    }
