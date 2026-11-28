"""Messenger channels over the chat core (#1020, epic #1018): Signal (#1032).

Each channel is a thin adapter: it resolves the sender to ONE bound patient
(fail closed, no default patient), calls ``oncofiles.chat.core.answer_text``
and sends the answer back. Nothing here starts without its own config.
"""
