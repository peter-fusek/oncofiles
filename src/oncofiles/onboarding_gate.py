"""Who may start self-service Drive onboarding (hook for #852).

#852 adds the server-side gate ``DRIVE_ONBOARDING`` = closed | invite | open
(invite list stored as keyed hashes, admins always pass). Until it lands this
hook allows every authenticated caller, which is today's behaviour: dashboard
sign-in is open signup and the "not open yet" state is copy only.

#852 replaces the body of ``onboarding_allowed`` and keeps the signature, so
the call sites (``/api/gdrive-set-folder`` before a curated-folder bootstrap,
#853) need no change. Never log the e-mail passed in.
"""

from __future__ import annotations


def onboarding_allowed(caller_email: str | None, *, is_admin: bool) -> bool:
    """True when this caller may connect a new Drive folder and bootstrap it.

    TODO(#852): enforce DRIVE_ONBOARDING (closed | invite | open); admins pass.
    """
    del caller_email, is_admin  # unused until #852; the e-mail is never logged
    return True


__all__ = ["onboarding_allowed"]
