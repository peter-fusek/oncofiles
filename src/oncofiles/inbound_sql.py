"""The predicates of the #950 / #952 inbound sweep, shared by production and the dry run.

The ``inbound_queue`` loop (``oncofiles.inbound_loop``) selects its work with
the SQL below, through the ``database._inbound_jobs`` /
``_inbound_claimed`` / ``_inbound_quarantine`` helpers.
``oncofiles.job_dryrun inbound_maintenance`` (the #999 "check before" tool)
runs the SAME statements and the same cap rules on a read-only copy of the
production DB, so its counts cannot drift from what the sweep does.

This module imports nothing from ``oncofiles`` (no config, no DB driver):
the dry run loads it without touching the environment or creating any
directory. Plain ``?`` parameters, valid for ``sqlite3`` and ``aiosqlite``.
"""

from __future__ import annotations

# ── inbound_jobs (#952) ──────────────────────────────────────────────────

# WHERE of a job the worker may claim now (params: now, stale_before): a
# pending job whose next_attempt_at has come, or a processing job not touched
# since stale_before (a crash or kill mid-way).
JOB_CLAIMABLE_WHERE = (
    "(state = 'pending' AND next_attempt_at <= ?) OR (state = 'processing' AND updated_at < ?)"
)
JOB_CLAIM_ORDER = "ORDER BY next_attempt_at, id"

# ── inbound_claimed_jobs (#950 DKIM, migration 099) ──────────────────────

# Same shape as the queue's (params: now, stale_before).
CLAIMED_CLAIMABLE_WHERE = JOB_CLAIMABLE_WHERE
CLAIMED_OPEN_STATES = "('pending', 'processing')"
CLAIMED_DEFERRED_STATES = "('deferred')"

# Deferred rows older than the Brevo attachment retention end
# failed/download_expired (param: expired_before).
CLAIMED_DEFERRED_EXPIRED_WHERE = "state = 'deferred' AND created_at < ?"
# Deferred rows in promotion order (oldest first).
CLAIMED_DEFERRED_SQL = (
    "SELECT id, bucket, sender_hash, domain_hash, created_at FROM inbound_claimed_jobs "
    "WHERE state = 'deferred' ORDER BY created_at, id"
)


def claimed_count_sql(states: str, where: str = "") -> str:
    """``SELECT COUNT(*)`` of claimed rows in ``states`` (+ an extra filter)."""
    sql = f"SELECT COUNT(*) FROM inbound_claimed_jobs WHERE state IN {states}"
    if where:
        sql += " AND " + where
    return sql


def claimed_checks(row: dict, *, deferred: bool) -> tuple:
    """(cap name, in-batch key, SQL filter, params) of every cap of ``row``.

    The welcome bucket and the address buckets are SEPARATE pools (security
    review of #966): a welcome@ flood from many Gmail senders can never use
    up the slots of the patients' addresses, nor the other way round.

    The sender caps count per (bucket, canonical sender) (infra review of
    #966): forgeries carrying the caregiver's From sent to the public welcome@
    never use the slots of that caregiver's real mail to the patient address.

    Open caps (``deferred=False``): ``sender`` (per bucket and canonical
    sender); for welcome@ ``welcome`` (its whole pool); for an address
    bucket ``address`` (per token), ``total`` (all address buckets) and
    ``domain`` (per From domain; never in welcome@: maintainer decision,
    every welcome@ sender is a Gmail user). Deferred caps:
    ``deferred_sender`` (per bucket and sender); for welcome@ ``deferred_welcome``; for an address
    bucket ``deferred_bucket`` (per token) and ``deferred_total`` (all
    address buckets).
    """
    bucket, sender, domain = row["bucket"], row["sender_hash"], row["domain_hash"]
    welcome = bucket == "welcome"
    if deferred:
        out = (
            (
                "deferred_sender",
                ("d_sender", bucket, sender),
                "bucket = ? AND sender_hash = ?",
                (bucket, sender),
            ),
        )
        if welcome:
            return out + (("deferred_welcome", ("d_welcome",), "bucket = 'welcome'", ()),)
        return out + (
            ("deferred_bucket", ("d_bucket", bucket), "bucket = ?", (bucket,)),
            ("deferred_total", ("d_total",), "bucket <> 'welcome'", ()),
        )
    out = (
        ("sender", ("sender", bucket, sender), "bucket = ? AND sender_hash = ?", (bucket, sender)),
    )
    if welcome:
        return out + (("welcome", ("welcome",), "bucket = 'welcome'", ()),)
    return out + (
        ("address", ("bucket", bucket), "bucket = ?", (bucket,)),
        ("total", ("total",), "bucket <> 'welcome'", ()),
        ("domain", ("domain", bucket, domain), "bucket = ? AND domain_hash = ?", (bucket, domain)),
    )


# ── inbound_quarantine (#950, migrations 101 / 102) ──────────────────────

# Pending items the expiry marks expired (params: before, stale_before).
QUARANTINE_EXPIRE_WHERE = (
    "state = 'pending' AND COALESCE(restored_at, received_at) < ? "
    "AND (claimed_at IS NULL OR claimed_at < ?)"
)

# Patients with pending items received since ``since`` that no notification
# announced yet (param: since).
QUARANTINE_PATIENTS_DUE_SQL = (
    "SELECT DISTINCT patient_id FROM inbound_quarantine "
    "WHERE state = 'pending' AND notified_at IS NULL AND received_at >= ?"
)
# One patient's unannounced pending items (params: patient_id, since);
# callers prepend the column list.
QUARANTINE_UNNOTIFIED_FROM = (
    "FROM inbound_quarantine "
    "WHERE patient_id = ? AND state = 'pending' AND notified_at IS NULL "
    "AND received_at >= ? ORDER BY received_at, id"
)
# The last notice of one sender to one patient (params: patient_id, sender_hash).
QUARANTINE_LAST_NOTICE_SQL = (
    "SELECT MAX(notified_at) FROM inbound_quarantine "
    "WHERE patient_id = ? AND sender_hash = ? AND notified_at IS NOT NULL"
)
# Notification e-mails sent for one patient since ``since`` (params: patient_id, since).
QUARANTINE_NOTICES_SINCE_SQL = (
    "SELECT COUNT(DISTINCT notice_key) FROM inbound_quarantine "
    "WHERE patient_id = ? AND notified_at >= ? AND notice_key IS NOT NULL"
)
# Items whose encrypted files are due for the purge
# (params: trash_before, approved_before, approved_before, limit).
QUARANTINE_PURGE_CANDIDATES_SQL = (
    "SELECT id, patient_id, item_uuid, state, file_count, filed_count, decided_at "
    "FROM inbound_quarantine "
    "WHERE files_purged_at IS NULL AND decided_at IS NOT NULL "
    "AND ((state IN ('rejected', 'expired') AND decided_at < ?) "
    "  OR (? IS NOT NULL AND state = 'approved' AND decided_at < ? "
    "      AND filed_count >= file_count)) "
    "ORDER BY decided_at, id LIMIT ?"
)


def due_for_notice(items: list[dict], last_notice_by_sender: dict, hour_ago: str) -> list[dict]:
    """The unannounced items one notification would announce: those whose
    sender got no notice since ``hour_ago`` (``notify_due`` and the dry run)."""
    due = []
    for it in items:
        last = last_notice_by_sender.get(it["sender_hash"])
        if last and last >= hour_ago:
            continue
        due.append(it)
    return due
