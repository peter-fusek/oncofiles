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

from oncofiles import inbound_email, onboarding_gate
from oncofiles.inbound_email import INBOUND_DOMAIN

WELCOME_ADDRESS = f"welcome@{INBOUND_DOMAIN}"

# Every one of these must be set before the landing may show the welcome
# address: the Brevo route token, the durable queue key (#952) and the holding
# store key. The sender verification must also be ready
# (``inbound_email.verification_ready``: DKIM only, it needs nothing extra;
# ``INBOUND_BREVO_AUTHSERV_ID`` is ignored and changes nothing here).
INBOUND_WELCOME_ENVS = (
    "INBOUND_BREVO_TOKEN",
    "INBOUND_JOBS_SECRET",
    "HOLDING_STORE_SECRET",
)

# Non-secret runtime switch (#950): the e-mail CTA turns on only when infra
# sets ``INBOUND_GO_LIVE=true``, even with every variable above already set
# (they are, in production), so infra decides the moment. Default off.
# It also gates welcome@ trial creation itself (``inbound_welcome.admit``,
# reason ``not_live``), not only the CTA; existing patients are not affected.
GO_LIVE_ENV = "INBOUND_GO_LIVE"

# Short browser/proxy cache: a mode flip shows up on the landing within a minute.
CACHE_SECONDS = 60


def go_live() -> bool:
    """True only for ``INBOUND_GO_LIVE=true`` (case-insensitive)."""
    return os.environ.get(GO_LIVE_ENV, "").strip().lower() == "true"


def inbound_welcome_enabled() -> bool:
    """True only when ``INBOUND_GO_LIVE`` is ``true``, every variable of
    ``INBOUND_WELCOME_ENVS`` is set and the sender verification is ready."""
    return (
        go_live()
        and all(os.environ.get(name, "").strip() for name in INBOUND_WELCOME_ENVS)
        and inbound_email.verification_ready()
    )


def public_config() -> dict:
    """The landing's public flags; env only, never the DB."""
    return {
        "inbound_welcome_enabled": inbound_welcome_enabled(),
        "onboarding_mode": onboarding_gate.onboarding_mode(),
        "welcome_address": WELCOME_ADDRESS,
    }
