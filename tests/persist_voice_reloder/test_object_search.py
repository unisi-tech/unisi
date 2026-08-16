"""
Tests for User.get_objects / User.get_contexts: general (namespace, path,
context_template) search across any persisted row -- positional (context_key
== ""), keyed (context_key from a key function), or otherwise. Both share
the same exact-vs-template rule for context_template (no '..' -> exact
match; contains '..' -> prefix/suffix pattern) and the same (namespace,
path) addressing that persist_location resolves for a given unit.
"""
import pytest


@pytest.mark.asyncio
async def test_get_objects_exact_match_returns_the_one_record(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "hello")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    found = user.get_objects(ns, path, "A")

    assert list(found.keys()) == ["A"]
    assert found["A"]["value"] == "hello"


@pytest.mark.asyncio
async def test_get_objects_exact_match_returns_empty_for_a_different_key(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "hello")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    assert user.get_objects(ns, path, "B") == {}


@pytest.mark.asyncio
async def test_get_objects_template_match_returns_all_matching_records(make_user, deliver):
    user = make_user("keyed")
    for city, zipc, note in [("London", "10001", "one"), ("London", "20002", "two"), ("Paris", "30003", "three")]:
        await deliver(user, "Root", "City", "changed", city)
        await deliver(user, "Root", "Zip", "changed", zipc)
        await deliver(user, "Root", "Multi key field", "changed", note)

    ns, path = user.persist_location(user.screen_module.multi_key_field)
    all_records = user.get_objects(ns, path, "..")

    assert set(all_records.keys()) == {"London,10001", "London,20002", "Paris,30003"}
    assert all_records["London,10001"]["value"] == "one"


@pytest.mark.asyncio
async def test_get_objects_prefix_template(make_user, deliver):
    user = make_user("keyed")
    for city, zipc, note in [("London", "10001", "one"), ("London", "20002", "two"), ("Paris", "30003", "three")]:
        await deliver(user, "Root", "City", "changed", city)
        await deliver(user, "Root", "Zip", "changed", zipc)
        await deliver(user, "Root", "Multi key field", "changed", note)

    ns, path = user.persist_location(user.screen_module.multi_key_field)
    london_only = user.get_objects(ns, path, "London,..")

    assert set(london_only.keys()) == {"London,10001", "London,20002"}


def test_get_objects_returns_empty_when_db_never_created(make_user):
    user = make_user("keyed")
    ns, path = user.persist_location(user.screen_module.single_key_field)
    assert user.get_objects(ns, path, "..") == {}
    assert user.db is None  # a pure read must not create a DB file


def test_get_objects_returns_empty_for_a_namespace_path_with_nothing_saved(make_user):
    user = make_user("positional")
    assert user.get_objects("NoSuchScreen", "no@such@path", "..") == {}


@pytest.mark.asyncio
async def test_get_contexts_returns_only_the_keys_not_the_fields(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "hello")
    await deliver(user, "Root", "Selector", "changed", "B")
    await deliver(user, "Root", "Single key field", "changed", "world")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    contexts = user.get_contexts(ns, path, "..")

    assert set(contexts) == {"A", "B"}
    assert all(isinstance(c, str) for c in contexts)


@pytest.mark.asyncio
async def test_get_contexts_exact_match(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "hello")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    assert user.get_contexts(ns, path, "A") == ["A"]
    assert user.get_contexts(ns, path, "nope") == []


def test_get_contexts_returns_empty_list_when_db_never_created(make_user):
    user = make_user("keyed")
    ns, path = user.persist_location(user.screen_module.single_key_field)
    assert user.get_contexts(ns, path, "..") == []


@pytest.mark.asyncio
async def test_positional_row_is_reachable_through_get_objects_with_empty_context_key(
    make_user, deliver
):
    user = make_user("positional")
    await deliver(user, "Root", "Flagged", "changed", "hi")

    ns, path = user.persist_location(user.screen_module.flagged)
    assert user.get_objects(ns, path, "")[""]["value"] == "hi"
    assert user.get_contexts(ns, path, "") == [""]
