"""
Tests for the simple, screen-independent key-value store: User.get_key /
set_key / get_keys / remove_key / remove_keys. This store shares the same
`state` table as unit persistence but always with namespace='' and path=''
-- a plain global scratchpad for the session, unrelated to any Unit's tree
position.
"""
import pytest


def test_get_key_returns_none_when_nothing_was_ever_saved(make_user):
    user = make_user("positional")
    assert user.get_key("missing") is None


def test_get_key_does_not_create_a_db_file_for_a_pure_read(make_user):
    user = make_user("positional")
    user.get_key("missing")
    assert user.db is None


def test_set_then_get_round_trips_a_string(make_user):
    user = make_user("positional")
    user.set_key("greeting", "hello")
    assert user.get_key("greeting") == "hello"


def test_set_then_get_round_trips_a_dict(make_user):
    # set_key/get_key aren't limited to plain strings -- anything
    # JSON-serializable survives the round trip.
    user = make_user("positional")
    user.set_key("prefs", {"theme": "dark", "volume": 7})
    assert user.get_key("prefs") == {"theme": "dark", "volume": 7}


def test_set_key_overwrites_a_previous_value(make_user):
    user = make_user("positional")
    user.set_key("k", "first")
    user.set_key("k", "second")
    assert user.get_key("k") == "second"


def test_keys_are_scoped_per_session(make_user):
    u1 = make_user("positional")
    u2 = make_user("positional")
    u1.set_key("shared-looking-name", "u1's value")
    assert u2.get_key("shared-looking-name") is None


def test_get_keys_requires_a_template_marker(make_user):
    user = make_user("positional")
    with pytest.raises(ValueError):
        user.get_keys("no-marker-here")


def test_get_keys_prefix_match(make_user):
    user = make_user("positional")
    user.set_key("user:1", "alice")
    user.set_key("user:2", "bob")
    user.set_key("other", "carol")
    assert user.get_keys("user:..") == {"user:1": "alice", "user:2": "bob"}


def test_get_keys_suffix_match(make_user):
    user = make_user("positional")
    user.set_key("a.tmp", 1)
    user.set_key("b.tmp", 2)
    user.set_key("c.dat", 3)
    assert user.get_keys("..tmp") == {"a.tmp": 1, "b.tmp": 2}


def test_get_keys_empty_when_nothing_matches(make_user):
    user = make_user("positional")
    user.set_key("x", 1)
    assert user.get_keys("nomatch..") == {}


def test_get_keys_empty_when_db_never_created(make_user):
    user = make_user("positional")
    assert user.get_keys("anything..") == {}
    assert user.db is None


def test_remove_key_returns_the_removed_value(make_user):
    user = make_user("positional")
    user.set_key("k", "value")
    assert user.remove_key("k") == "value"
    assert user.get_key("k") is None


def test_remove_key_returns_none_when_key_did_not_exist(make_user):
    user = make_user("positional")
    assert user.remove_key("never-set") is None


def test_remove_keys_requires_a_template_marker(make_user):
    user = make_user("positional")
    with pytest.raises(ValueError):
        user.remove_keys("no-marker-here")


def test_remove_keys_deletes_only_matches_and_returns_them(make_user):
    user = make_user("positional")
    user.set_key("user:1", "alice")
    user.set_key("user:2", "bob")
    user.set_key("other", "carol")

    removed = user.remove_keys("user:..")

    assert removed == {"user:1": "alice", "user:2": "bob"}
    assert user.get_key("user:1") is None
    assert user.get_key("user:2") is None
    assert user.get_key("other") == "carol"  # untouched
