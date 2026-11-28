"""Chat with one patient's records on the local model (#1020, epic #1018).

``oncofiles.chat.core.answer`` is the channel-agnostic core: one patient per
call, read-only tools called in-process, the local model only, a grounding
guard and citations from tool results. Front ends (dashboard #1023, demo
#1024, WhatsApp #626) are thin adapters that check the ACL first.

This package ``__init__`` stays import-free on purpose: ``chat.scope`` is
imported by ``tools._helpers`` (the patient resolver honours the chat scope),
so pulling in the core here would create an import cycle.
"""
