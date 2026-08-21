# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/graphs.py: Node, Edge, Graph, Net, unit2image and the
Topology factory.

Regression coverage for the two bugs fixed alongside this suite:

  * Graph's `.value` default and Net's `value=`/`topology=` parameter
    defaults were all the *same* module-level mutable object
    (`graph_default_value`, `Topology()` evaluated once at class-definition
    time) shared across every instance that didn't override them --
    see TestGraphDefaultValueIsolation / TestNetDefaultIsolation.
"""
import pytest

from unisi.units import (
    Unit, Button, Edit, Text, Switch, TextArea, Tree, Select, Range, HTML,
)
from unisi.containers import Block
from unisi.tables import Table
from unisi.graphs import Node, Edge, Graph, Net, Topology, unit2image, graph_default_value


# ──────────────────────────────────────────────────────────────────────── #
#  Node                                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestNode:
    def test_name_only(self):
        n = Node('Alice')
        assert n.name == 'Alice'
        assert n.type == ''
        assert not hasattr(n, 'color')
        assert not hasattr(n, 'size')

    def test_empty_name_is_not_set(self):
        """Documents current behaviour: falsy fields (name='', color='',
        size=0) are omitted entirely rather than stored as empty/zero, to
        keep the serialised payload small -- consistent with Edge below."""
        n = Node('')
        assert not hasattr(n, 'name')

    def test_image_sets_type_and_image(self):
        n = Node('Alice', image='alice.png')
        assert n.type == 'image'
        assert n.image == 'alice.png'

    def test_color_and_size(self):
        n = Node('Alice', color='red', size=20)
        assert n.color == 'red'
        assert n.size == 20

    def test_zero_size_is_not_set(self):
        n = Node('Alice', size=0)
        assert not hasattr(n, 'size')


# ──────────────────────────────────────────────────────────────────────── #
#  Edge                                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestEdge:
    def test_source_and_target_always_set(self):
        e = Edge(0, 1)
        assert e.source == 0
        assert e.target == 1

    def test_optional_fields_omitted_when_falsy(self):
        e = Edge(0, 1)
        assert not hasattr(e, 'name')
        assert not hasattr(e, 'color')
        assert not hasattr(e, 'size')
        assert not hasattr(e, 'property')

    def test_optional_fields_set_when_provided(self):
        e = Edge(0, 1, name='knows', color='blue', size=2, property={'weight': 5})
        assert e.name == 'knows'
        assert e.color == 'blue'
        assert e.size == 2
        assert e.property == {'weight': 5}

    def test_property_none_is_not_set_but_falsy_dict_is(self):
        """`property` uses an `is not None` check (unlike name/color/size,
        which use plain truthiness), so an explicit empty dict IS kept."""
        e = Edge(0, 1, property={})
        assert e.property == {}

    def test_str_and_repr(self):
        e = Edge(2, 5)
        assert str(e) == 'Edge(2->5)'
        assert repr(e) == 'Edge(2->5)'


# ──────────────────────────────────────────────────────────────────────── #
#  Graph                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestGraph:
    def test_defaults(self):
        g = Graph('g')
        assert g.type == 'graph'
        assert g.value == {'nodes': [], 'edges': []}
        assert g.nodes == []
        assert g.edges == []

    def test_explicit_value_is_respected(self):
        g = Graph('g', value={'nodes': [1], 'edges': []})
        assert g.value == {'nodes': [1], 'edges': []}

    def test_regression_two_instances_do_not_share_default_value(self):
        """Regression: every Graph() built without an explicit value= used
        to receive the *same* module-level dict object as its .value.
        Mutating one instance's nodes/edges list used to silently show up
        on every other instance."""
        g1 = Graph('g1')
        g2 = Graph('g2')
        assert g1.value is not g2.value
        g1.value['nodes'].append('node_x')
        assert g2.value == {'nodes': [], 'edges': []}

    def test_graph_default_value_constant_is_untouched(self):
        """The module-level constant itself must stay pristine -- it's
        still part of the public API (documents the expected shape) even
        though it's no longer wired in as a live, shared default."""
        Graph('g')
        assert graph_default_value == {'nodes': [], 'edges': []}


# ──────────────────────────────────────────────────────────────────────── #
#  unit2image                                                              #
# ──────────────────────────────────────────────────────────────────────── #

class TestUnit2Image:
    def test_edit_number_and_string_use_different_icons(self):
        number_icon = unit2image(Edit('n', 5))
        string_icon = unit2image(Edit('n', 'x'))
        assert number_icon != string_icon
        assert number_icon and string_icon  # both non-empty URLs

    def test_text_uses_the_same_family_as_edit_string(self):
        assert unit2image(Text('t')) == unit2image(Edit('e', 'x'))

    def test_button_switch_textarea_tree_select_have_distinct_icons(self):
        icons = {
            'button': unit2image(Button('b')),
            'switch': unit2image(Switch('s')),
            'textarea': unit2image(TextArea('t')),
            'tree': unit2image(Tree('t')),
            'select': unit2image(Select('s')),
            'range': unit2image(Range('r')),
        }
        assert all(icons.values())
        assert len(set(icons.values())) == len(icons)  # all different

    def test_table_type_table_vs_other_type(self):
        plain_table_icon = unit2image(Table('t'))
        chart_like = Table('t2')
        chart_like.type = 'not-table'
        assert unit2image(chart_like) != plain_table_icon

    def test_graph_and_generic_unit_have_icons(self):
        assert unit2image(Graph('g'))
        assert unit2image(Unit('plain'))

    def test_net_matches_graph_case_via_inheritance(self):
        assert unit2image(Net('n')) == unit2image(Graph('g'))

    def test_block_has_its_own_icon(self):
        icon = unit2image(Block('b', Unit('child')))
        assert icon and icon != unit2image(Unit('plain'))

    def test_unmatched_type_returns_empty_string(self):
        assert unit2image(object()) == ''

    def test_any_unit_subclass_without_a_specific_case_falls_back_to_generic_unit_icon(self):
        """HTML has no dedicated `case HTML():` branch, but it's still a
        Unit, so it matches the generic `case Unit():` fallback (which
        returns a real icon) rather than the final `case _:` (empty
        string) -- that branch is only reachable for non-Unit objects."""
        assert unit2image(HTML('h', '<p></p>')) == unit2image(Unit('plain'))


# ──────────────────────────────────────────────────────────────────────── #
#  Net                                                                     #
# ──────────────────────────────────────────────────────────────────────── #

class TestNetConstruction:
    def test_defaults_with_no_topology(self):
        n = Net('n')
        assert n.type == 'graph'
        assert n.value == {'nodes': [], 'edges': []}
        assert n._nodes == []
        assert n._edges == []
        assert n._narray == []

    def test_regression_two_instances_do_not_share_topology(self):
        """Regression: Net's `topology=Topology()` default parameter used
        to be evaluated exactly once, at function-definition time -- every
        Net() built without an explicit topology= ended up mutating the
        *same* nested defaultdict."""
        n1 = Net('n1')
        n2 = Net('n2')
        assert n1.topology is not n2.topology
        a, b = Unit('a'), Unit('b')
        n1.topology[a][b] = True
        assert list(n2.topology.items()) == []

    def test_regression_two_instances_do_not_share_value(self):
        n1 = Net('n1')
        n2 = Net('n2')
        assert n1.value is not n2.value

    def test_topology_is_a_fresh_nested_defaultdict_by_default(self):
        n = Net('n')
        a, b = Unit('a'), Unit('b')
        # never touched -> defaultdict autovivifies rather than KeyError
        assert n.topology[a][b] is None


class TestNetBuildFromTopology:
    def test_builds_nodes_and_edges_from_topology(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        assert [node.name for node in n._nodes] == ['Alice', 'Bob']
        assert n._narray == [alice, bob]
        assert len(n._edges) == 1
        assert n._edges[0].source == 0 and n._edges[0].target == 1

    def test_shared_unit_across_edges_is_not_duplicated_in_narray(self):
        hub, a, b = Unit('Hub'), Unit('A'), Unit('B')
        topo = Topology()
        topo[hub][a] = True
        topo[hub][b] = True
        n = Net('n', topology=topo)
        assert len(n._narray) == 3  # hub, a, b -- hub only counted once
        assert n._narray.count(hub) == 1
        assert len(n._edges) == 2

    def test_elements_returns_the_narray(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        assert n.elements() == n._narray


class TestNetChangedConversion:
    def test_changed_resolves_indices_to_units_and_edges(self, fake_user):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        n.set_reactivity(fake_user)

        n.changed(n, {'nodes': [0, 1], 'edges': [0]})

        assert n.selected == {'nodes': [0, 1], 'edges': [0]}
        assert n.value['nodes'] == [alice, bob]
        assert len(n.value['edges']) == 1
        resolved_edge = n.value['edges'][0]
        assert resolved_edge.source is alice
        assert resolved_edge.target is bob

    def test_changed_composes_with_a_user_supplied_handler(self, fake_user):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        received = []
        n = Net('n', topology=topo, changed=lambda obj, val: received.append(val))
        n.set_reactivity(fake_user)

        n.changed(n, {'nodes': [0], 'edges': []})

        assert len(received) == 1
        assert received[0]['nodes'] == [alice]

    def test_changed_with_empty_selection(self, fake_user):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        n.set_reactivity(fake_user)
        n.changed(n, {'nodes': [], 'edges': []})
        assert n.value == {'nodes': [], 'edges': []}

    def test_stale_raw_index_value_is_normalised_when_a_changed_handler_is_present(self, fake_user):
        """When a user-supplied `changed` handler is passed, changed_converter
        doesn't overwrite self.value with the newly-resolved selection
        itself (it hands that to the user's handler instead) -- but it
        still normalises a *stale* self.value that's still holding raw
        int node indices (e.g. what a caller might pass as the initial
        `value=` before any real selection has happened) into the
        Unit-based shape first."""
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        received = []
        n = Net(
            'n', topology=topo,
            value={'nodes': [0], 'edges': []},  # stale: raw index, not a Unit
            changed=lambda obj, val: received.append(val),
        )
        n.set_reactivity(fake_user)

        n.changed(n, {'nodes': [1], 'edges': []})

        assert n.value == {'nodes': [alice], 'edges': []}
        assert received == [{'nodes': [bob], 'edges': []}]


class TestNetGetstate:
    def test_getstate_excludes_topology_and_raw_value(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        state = n.__getstate__()
        assert 'topology' not in state
        assert state['nodes'] is n._nodes
        assert state['edges'] is n._edges

    def test_getstate_value_defaults_to_empty_shape_before_any_change_event(self):
        n = Net('n')
        state = n.__getstate__()
        assert state['value'] == {'nodes': [], 'edges': []}

    def test_getstate_value_uses_raw_selected_after_a_change_event(self, fake_user):
        alice, bob = Unit('Alice'), Unit('Bob')
        topo = Topology()
        topo[alice][bob] = True
        n = Net('n', topology=topo)
        n.set_reactivity(fake_user)
        n.changed(n, {'nodes': [0], 'edges': []})
        state = n.__getstate__()
        assert state['value'] == {'nodes': [0], 'edges': []}

    def test_regression_getstate_fallback_does_not_leak_the_shared_default(self):
        """Two Net()s that never had a change event both fall back to the
        empty-shape default in __getstate__ -- confirms that fallback is a
        fresh dict per call now, not a shared reference a caller could
        mutate into every other instance's state."""
        n1, n2 = Net('n1'), Net('n2')
        state1 = n1.__getstate__()
        state2 = n2.__getstate__()
        assert state1['value'] is not state2['value']
        state1['value']['nodes'].append('x')
        assert n2.__getstate__()['value'] == {'nodes': [], 'edges': []}


# ──────────────────────────────────────────────────────────────────────── #
#  Net.make_topology                                                       #
# ──────────────────────────────────────────────────────────────────────── #

class TestNetMakeTopology:
    def test_flat_iterable_creates_a_union_hub_node(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        n = Net('n')
        n.make_topology([alice, bob])
        # a synthetic 'Union' hub connects to each item in the iterable
        names = [node.name for node in n._nodes]
        assert 'Union' in names
        assert 'Alice' in names and 'Bob' in names
        assert len(n._edges) == 2

    def test_falsy_items_in_iterable_are_skipped(self):
        alice = Unit('Alice')
        n = Net('n')
        n.make_topology([alice, None, False, 0])
        assert len(n._narray) == 2  # the Union hub + Alice only

    def test_bare_leaf_unit_at_the_top_level_produces_an_empty_topology(self):
        """make_topology is designed to be called with a container (list or
        Block) at the top level. dive() only has a side effect (populating
        topo) for the Iterable/Block cases; for a bare, non-container Unit
        passed directly, the top-level dive() call hits the `case _`
        no-op and its return value is discarded, so nothing gets added to
        topo at all -- an empty graph, not a single-node one."""
        alice = Unit('Alice')
        n = Net('n')
        n.make_topology(alice)
        assert n._nodes == []
        assert n._edges == []

    def test_nested_iterables_create_nested_union_hubs(self):
        alice, bob, carol = Unit('Alice'), Unit('Bob'), Unit('Carol')
        n = Net('n')
        n.make_topology([alice, [bob, carol]])
        names = [node.name for node in n._nodes]
        assert names.count('Union') == 2  # outer hub + inner hub
        assert 'Alice' in names and 'Bob' in names and 'Carol' in names
        # outer->alice, outer->inner_hub, inner_hub->bob, inner_hub->carol
        assert len(n._edges) == 4

    def test_block_connects_to_each_of_its_child_elements(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        block = Block('container', alice, bob)
        n = Net('n')
        n.make_topology(block)
        names = [node.name for node in n._nodes]
        assert 'container' in names
        assert 'Alice' in names and 'Bob' in names
        assert len(n._edges) == 2

    def test_make_topology_replaces_any_previous_topology(self):
        alice, bob = Unit('Alice'), Unit('Bob')
        n = Net('n')
        n.make_topology([alice])
        first_narray_len = len(n._narray)
        n.make_topology([bob])
        assert len(n._narray) == 2  # fresh Union hub + bob, alice's gone
        assert 'Alice' not in [node.name for node in n._nodes]
