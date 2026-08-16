"""
Tests for voicecom.py's VoiceCom state machine. No audio or network is
involved anywhere in this module -- speech-to-text happens client-side and
only the recognized *words* ever reach VoiceCom (via process_word /
process_string), so these are all plain, synchronous-in-spirit state-machine
tests against a real User/screen, same fixtures_app pattern as the rest of
this suite.
"""
import pytest

from unisi.voicecom import VoiceCom, find_most_similar_sequence, word_to_number


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestWordToNumber:
    def test_digit_string(self):
        assert word_to_number("42") == 42.0

    def test_digit_string_with_thousands_comma(self):
        assert word_to_number("1,200") == 1200.0

    def test_spelled_out_number(self):
        assert word_to_number("forty") == 40.0

    def test_negative_digit_string(self):
        assert word_to_number("-5") == -5.0

    def test_garbage_returns_none(self):
        assert word_to_number("banana") is None

    def test_empty_string_returns_none(self):
        assert word_to_number("") is None


class TestFindMostSimilarSequence:
    def test_exact_match_ratio_is_one(self):
        match, ratio = find_most_similar_sequence("apple", ["apple", "banana"])
        assert match == "apple"
        assert ratio == 1.0

    def test_case_insensitive(self):
        match, ratio = find_most_similar_sequence("APPLE", ["apple", "banana"])
        assert match == "apple"
        assert ratio == 1.0

    def test_picks_the_closer_candidate(self):
        match, _ = find_most_similar_sequence("aple", ["apple", "orange", "grape"])
        assert match == "apple"

    def test_empty_candidate_list_returns_empty_result(self):
        assert find_most_similar_sequence("anything", []) == ("", 0.0)


# ---------------------------------------------------------------------------
# Indexing / lifecycle
# ---------------------------------------------------------------------------


def test_starts_in_root_mode_with_no_active_unit(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    assert voice.mode == "root"
    assert voice.unit is None


def test_indexes_editable_units_sorted_by_pretty_name(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    assert voice.unit_names == sorted(voice.unit_names)
    assert "Text field" in voice.unit_names
    assert "Data table" in voice.unit_names


def test_readonly_unit_is_not_indexed(make_user):
    # readonly_field has edit=False in the fixture screen
    user = make_user("voice_target")
    voice = VoiceCom(user)
    assert "Readonly field" not in voice.unit_names
    assert "Readonly field" not in voice.name2unit


def test_mates_own_block_is_never_indexed(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    assert "Recognized words" not in voice.unit_names
    assert "System message" not in voice.unit_names


def test_start_adds_the_mate_block_to_screen_blocks(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    assert voice.block not in user.screen.blocks

    voice.start()

    assert voice.block in user.screen.blocks


def test_start_is_idempotent(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.start()
    voice.start()
    assert list(user.screen.blocks).count(voice.block) == 1


def test_stop_removes_the_mate_block(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.start()

    voice.stop()

    assert voice.block not in user.screen.blocks


def test_reset_returns_to_root_and_clears_the_active_unit(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])
    assert voice.mode == "text"

    voice.reset()

    assert voice.mode == "root"
    assert voice.unit is None
    assert voice.input.value == ""


# ---------------------------------------------------------------------------
# activate_unit / set_mode
# ---------------------------------------------------------------------------


def test_activate_unit_sets_active_and_focus(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    unit = voice.name2unit["Text field"]

    voice.activate_unit(unit)

    assert voice.unit is unit
    assert unit.active is True
    assert unit.focus is True


def test_activate_unit_deactivates_the_previous_one(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    first = voice.name2unit["Text field"]
    second = voice.name2unit["Number field"]

    voice.activate_unit(first)
    voice.activate_unit(second)

    assert first.active is False
    assert first.focus is False
    assert voice.unit is second


def test_activate_unit_with_none_sets_a_not_found_message(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(None)
    assert voice.message.value == "Element not found"


@pytest.mark.parametrize(
    "field_name, expected_mode",
    [
        ("Text field", "text"),
        ("Number field", "number"),
        ("Flag field", "switch"),
        ("Color field", "radio"),
        ("Data table", "table"),
        ("Net field", "graph"),
        ("Go button", "command"),
    ],
)
def test_activating_each_field_type_enters_the_right_mode(make_user, field_name, expected_mode):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit[field_name])
    assert voice.mode == expected_mode


def test_set_mode_populates_command_list_for_the_mode(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])
    assert "backspace" in voice.command_list.options
    assert "enter" in voice.command_list.options


# ---------------------------------------------------------------------------
# Root-command escape hatch (must work from every mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["Text field", "Number field", "Flag field", "Data table"])
async def test_root_command_escapes_from_any_mode(make_user, field_name):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit[field_name])

    await voice.process_word("root")

    assert voice.mode == "root"
    assert voice.unit is None


@pytest.mark.asyncio
async def test_strict_mode_rejects_a_synonym_escape_word(make_user):
    # "table" is a STRICT_MODE: only the canonical word ("select" etc, not a
    # synonym) triggers the escape, so a real header/value named e.g. "select"
    # in a strict mode isn't accidentally swallowed as navigation.
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    # "choose" is a synonym for "root", not the canonical word itself
    await voice.process_word("choose")

    assert voice.mode == "table"  # did NOT escape


@pytest.mark.asyncio
async def test_strict_mode_still_accepts_the_canonical_escape_word(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_word("root")

    assert voice.mode == "root"


@pytest.mark.asyncio
async def test_non_strict_mode_accepts_a_synonym_escape_word(make_user):
    # switch mode is not in STRICT_MODES (STRICT_MODES = {"graph", "net",
    # "table", "text", "number"}), so a synonym works too there
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])

    await voice.process_word("choose")  # synonym for "root"

    assert voice.mode == "root"


# ---------------------------------------------------------------------------
# Text mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_mode_inserts_a_word_with_trailing_space(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])

    await voice.process_word("hello")

    assert voice.unit.value == "hello "


@pytest.mark.asyncio
async def test_text_mode_accumulates_multiple_words(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])

    await voice.process_string("hello world")

    assert voice.unit.value == "hello world "


@pytest.mark.asyncio
async def test_text_mode_double_tap_runs_the_command_instead_of_inserting_it(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])
    await voice.process_string("hello world")

    await voice.process_word("backspace")
    await voice.process_word("backspace")  # double-tap -> runs backspace, doesn't insert the word

    assert voice.unit.value == "hello world"
    assert "backspace" not in voice.unit.value


@pytest.mark.asyncio
async def test_text_command_clean_empties_the_field(make_user):
    # text-mode commands only run on a double-tap (the same word spoken
    # twice in a row) -- a single "clean" is just inserted as a literal
    # word instead (see test_text_mode_double_tap_runs_the_command_instead_of_inserting_it).
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])
    await voice.process_string("hello world")

    await voice.process_word("clean")
    await voice.process_word("clean")

    assert voice.unit.value == ""
    assert voice.unit.x == 0


@pytest.mark.asyncio
async def test_text_command_undo_reverts_only_the_undo_words_own_insertion(make_user):
    # Double-tap "undo" restores to whatever the value was right before the
    # *first* "undo" was typed -- i.e. it cancels out its own insertion, not
    # a multi-step edit history. Saying "hello world" then double-tapping
    # undo lands back on "hello world ", not further back on "hello ".
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Text field"])
    await voice.process_string("hello world")
    before_undo = voice.unit.value

    await voice.process_word("undo")
    await voice.process_word("undo")  # double-tap

    assert voice.unit.value == before_undo


# ---------------------------------------------------------------------------
# Number mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_number_mode_sets_a_spoken_number(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Number field"])

    await voice.process_word("forty")

    assert voice.unit.value == 40.0


@pytest.mark.asyncio
async def test_number_mode_rejects_a_non_number_word(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Number field"])

    await voice.process_word("banana")

    assert voice.message.value == "Not a number"
    assert voice.unit.value == 0  # unchanged


@pytest.mark.asyncio
async def test_number_mode_clean_command_clears_the_value(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Number field"])
    await voice.process_word("forty")

    await voice.process_word("clean")

    assert voice.unit.value is None


# ---------------------------------------------------------------------------
# Choice mode (switch / select / radio / list / tree)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_choice_mode_exact_word_applies_immediately_without_confirmation(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])

    await voice.process_word("true")

    assert voice.unit.value is True
    assert voice.context_list.value is None  # never needed staging for confirmation


@pytest.mark.asyncio
async def test_choice_mode_switch_maps_yes_and_on_to_true_too(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])
    await voice.process_word("yes")
    assert voice.unit.value is True

    voice.activate_unit(voice.name2unit["Flag field"])
    await voice.process_word("off")
    assert voice.unit.value is False


@pytest.mark.asyncio
async def test_choice_mode_partial_word_stages_for_confirmation(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])
    voice.unit.value = True

    await voice.process_word("fal")  # partial match for "false", not exact

    assert voice.context_list.value == "false"
    assert voice.unit.value is True  # not applied yet


@pytest.mark.asyncio
async def test_choice_mode_ok_confirms_the_staged_value(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])
    voice.unit.value = True
    await voice.process_word("fal")

    await voice.process_word("ok")

    assert voice.unit.value is False


@pytest.mark.asyncio
async def test_choice_mode_ok_with_nothing_staged_shows_a_message(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Flag field"])

    await voice.process_word("ok")

    assert voice.message.value == "Nothing to confirm"


@pytest.mark.asyncio
async def test_choice_mode_push_dispatches_to_the_changed_handler(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Toggle field"])

    await voice.process_word("push")

    assert ("switch_changed", False) in mod.voice_log


# ---------------------------------------------------------------------------
# Root mode (element selection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_mode_exact_name_activates_immediately(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)

    await voice.process_string("text field")

    assert voice.unit is voice.name2unit["Text field"]
    assert voice.mode == "text"


@pytest.mark.asyncio
async def test_root_mode_partial_name_needs_ok_to_confirm(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)

    await voice.process_word("text")  # "text" alone isn't "text field" verbatim

    assert voice.unit is None  # not yet activated
    assert voice.context_list.value == "Text field"

    await voice.process_word("ok")

    assert voice.unit is voice.name2unit["Text field"]


@pytest.mark.asyncio
async def test_root_mode_unmatched_word_leaves_root_mode_active(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)

    await voice.process_word("zzz_no_such_element_zzz")

    assert voice.mode == "root"
    assert voice.unit is None


# ---------------------------------------------------------------------------
# Screen mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_command_enters_screen_mode(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)

    await voice.process_word("screen")

    assert voice.mode == "screen"


@pytest.mark.asyncio
async def test_screen_mode_switches_the_current_screen(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    await voice.process_word("screen")

    await voice.process_string("positional")

    assert user.screen.name == "Positional"


# ---------------------------------------------------------------------------
# Table mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_next_and_prev_move_the_row_cursor(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_word("next")
    assert voice._table_row == 1

    await voice.process_word("prev")
    assert voice._table_row == 0


@pytest.mark.asyncio
async def test_table_next_does_not_go_past_the_last_row(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    for _ in range(10):
        await voice.process_word("next")

    assert voice._table_row == len(user.screen_module.data_table.rows) - 1


@pytest.mark.asyncio
async def test_table_multi_word_column_name_selects_the_column(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_string("col a")

    assert voice._table_col == 0


@pytest.mark.asyncio
async def test_table_edit_then_a_word_writes_the_cell(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])
    await voice.process_string("col a")

    await voice.process_word("edit")
    assert voice._table_editing is True

    await voice.process_word("newvalue")

    assert voice._table_editing is False
    assert mod.data_table.rows[0][0] == "newvalue"


@pytest.mark.asyncio
async def test_table_row_command_dispatches_to_the_changed_handler(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_word("row")

    assert ("table_changed", 0) in mod.voice_log


@pytest.mark.asyncio
async def test_table_delete_command_dispatches_to_the_delete_handler(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_word("delete")

    assert ("table_deleted", 0) in mod.voice_log


@pytest.mark.asyncio
async def test_table_confirm_command_dispatches_to_the_update_handler(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Data table"])

    await voice.process_word("confirm")

    assert ("table_updated", (0, 0)) in mod.voice_log


# ---------------------------------------------------------------------------
# Command mode (Button)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_mode_push_triggers_the_button(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Go button"])

    await voice.process_word("push")

    assert ("command", None) in mod.voice_log


# ---------------------------------------------------------------------------
# Graph mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_mode_selects_a_node_by_name(make_user):
    user = make_user("voice_target")
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Net field"])

    await voice.process_word("alpha")

    assert voice.unit.value["nodes"] == [0]  # "Alpha" is node index 0


@pytest.mark.asyncio
async def test_graph_add_node_command_appends_a_new_node(make_user):
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Net field"])
    before = len(mod.net_field.nodes)

    await voice.process_word("add")
    await voice.process_word("gamma")

    assert len(mod.net_field.nodes) == before + 1
    assert mod.net_field.nodes[-1].name == "gamma"


@pytest.mark.asyncio
async def test_graph_add_node_works_on_a_graph_that_starts_with_zero_nodes(make_user):
    # companion to the edges-side fix -- same "is None vs empty" issue
    # applied to _graph_nodes()/_graph_add_node.
    from unisi.graphs import Graph

    user = make_user("voice_target")
    voice = VoiceCom(user)
    g = Graph("Empty graph", nodes=[], edges=[])
    voice.unit = g
    voice.mode = "graph"
    voice._refresh_graph_context()

    await voice.process_word("add")
    await voice.process_word("first")

    assert len(g.nodes) == 1
    assert g.nodes[0].name == "first"


@pytest.mark.asyncio
async def test_graph_connect_command_adds_an_edge_between_two_nodes(make_user):
    # _graph_edges() used to do `getattr(unit, "edges", None) or
    # getattr(unit, "_edges", [])` -- an empty list is falsy, so a graph
    # that starts with edges=[] (the natural default for a brand new
    # graph, as this fixture's net_field does) made that `or` fall through
    # to a nonexistent `_edges` attribute, silently appending the new Edge
    # to a throwaway list instead of the unit's real one. Fixed to check
    # `is None` instead (see voicecom.py) so this now works correctly even
    # from a graph with zero edges to start.
    user = make_user("voice_target")
    mod = user.screen_module
    voice = VoiceCom(user)
    voice.activate_unit(voice.name2unit["Net field"])
    await voice.process_word("alpha")  # select source node

    await voice.process_word("connect")
    await voice.process_word("beta")  # target

    assert len(mod.net_field.edges) == 1
    assert mod.net_field.edges[0].source == 0
    assert mod.net_field.edges[0].target == 1


@pytest.mark.asyncio
async def test_graph_connect_logic_also_works_when_the_edges_list_starts_non_empty(make_user):
    from unisi.graphs import Graph, Node, Edge

    user = make_user("voice_target")
    voice = VoiceCom(user)
    # a standalone unit with a non-empty starting edges list, activated
    # directly (bypassing the fixture screen's own net_field) -- covering
    # the "already had edges" case alongside the "starts empty" case above
    g = Graph("Standalone", nodes=[Node("X"), Node("Y"), Node("Z")], edges=[Edge(0, 1)])
    voice.unit = g
    voice.mode = "graph"
    voice._refresh_graph_context()

    await voice.process_word("x")
    await voice.process_word("connect")
    await voice.process_word("z")

    assert len(g.edges) == 2
    assert (g.edges[1].source, g.edges[1].target) == (0, 2)


# ---------------------------------------------------------------------------
# User-level wiring (process() routing, lazy creation, screen-follow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_is_lazily_created_on_first_voice_message(make_user):
    from unisi.common import ReceivedMessage

    user = make_user("voice_target")
    assert user.voice is None

    msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": True})
    await user.process(msg)

    assert user.voice is not None
    assert isinstance(user.voice, VoiceCom)


@pytest.mark.asyncio
async def test_listen_true_starts_and_listen_false_stops(make_user):
    from unisi.common import ReceivedMessage

    user = make_user("voice_target")
    start_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": True})
    await user.process(start_msg)
    assert user.voice.block in user.screen.blocks

    stop_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": False})
    await user.process(stop_msg)
    assert user.voice.block not in user.screen.blocks


@pytest.mark.asyncio
async def test_non_listen_voice_message_is_routed_to_process_string(make_user):
    from unisi.common import ReceivedMessage

    user = make_user("voice_target")
    start_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": True})
    await user.process(start_msg)

    speech_msg = ReceivedMessage(
        {"block": "voice", "element": None, "event": "speech", "value": "text field"}
    )
    await user.process(speech_msg)

    assert user.voice.unit is user.voice.name2unit["Text field"]


@pytest.mark.asyncio
async def test_voice_follows_the_user_to_a_new_screen(make_user, deliver):
    user = make_user("voice_target")
    from unisi.common import ReceivedMessage

    start_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": True})
    await user.process(start_msg)
    assert user.voice.block in user.screen.blocks

    await deliver(user, "root", None, "changed", "Positional")

    assert user.screen.name == "Positional"
    assert user.voice.screen_name == "Positional"
    assert user.voice.block in user.screen.blocks
