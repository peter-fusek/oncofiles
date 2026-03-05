"""Tests for agent state key-value store (#32)."""

from erika_files_mcp.database import Database

from .helpers import make_agent_state


async def test_set_and_get(db: Database):
    state = make_agent_state(key="protocol", value='{"name": "FOLFOX"}')
    saved = await db.set_agent_state(state)
    assert saved.id is not None
    assert saved.key == "protocol"
    assert saved.value == '{"name": "FOLFOX"}'

    fetched = await db.get_agent_state("protocol")
    assert fetched is not None
    assert fetched.value == '{"name": "FOLFOX"}'


async def test_get_not_found(db: Database):
    result = await db.get_agent_state("nonexistent")
    assert result is None


async def test_upsert_updates_value(db: Database):
    await db.set_agent_state(make_agent_state(key="counter", value="1"))
    await db.set_agent_state(make_agent_state(key="counter", value="2"))

    fetched = await db.get_agent_state("counter")
    assert fetched.value == "2"


async def test_upsert_returns_updated(db: Database):
    await db.set_agent_state(make_agent_state(key="k", value="old"))
    saved = await db.set_agent_state(make_agent_state(key="k", value="new"))
    assert saved.value == "new"


async def test_different_agents_separate_keys(db: Database):
    await db.set_agent_state(make_agent_state(agent_id="a1", key="x", value="1"))
    await db.set_agent_state(make_agent_state(agent_id="a2", key="x", value="2"))

    a1 = await db.get_agent_state("x", agent_id="a1")
    a2 = await db.get_agent_state("x", agent_id="a2")
    assert a1.value == "1"
    assert a2.value == "2"


async def test_list_agent_states(db: Database):
    await db.set_agent_state(make_agent_state(key="b_key", value="2"))
    await db.set_agent_state(make_agent_state(key="a_key", value="1"))

    states = await db.list_agent_states()
    assert len(states) == 2
    assert states[0].key == "a_key"  # sorted by key
    assert states[1].key == "b_key"


async def test_list_empty(db: Database):
    states = await db.list_agent_states()
    assert states == []


async def test_list_filters_by_agent(db: Database):
    await db.set_agent_state(make_agent_state(agent_id="oncoteam", key="k1"))
    await db.set_agent_state(make_agent_state(agent_id="other", key="k2"))

    states = await db.list_agent_states(agent_id="oncoteam")
    assert len(states) == 1
    assert states[0].key == "k1"


async def test_timestamps_set(db: Database):
    saved = await db.set_agent_state(make_agent_state(key="ts"))
    assert saved.created_at is not None
    assert saved.updated_at is not None
