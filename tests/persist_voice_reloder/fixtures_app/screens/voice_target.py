from unisi import *
from unisi.graphs import Graph, Node, Edge

name = "VoiceTarget"
order = 4

# a log every handler below appends to, so tests can see exactly what
# voice command dispatch actually triggered
voice_log = []

text_field = Edit("Text field", "")
number_field = Range("Number field", 0, options=[0, 100, 1])
flag_field = Switch("Flag field", False)
color_field = Select("Color field", "red", options=["red", "green", "blue"])
readonly_field = Edit("Readonly field", "not editable", edit=False)  # must be skipped by indexing


async def table_changed(unit, value):
    voice_log.append(("table_changed", value))


async def table_deleted(unit, value):
    voice_log.append(("table_deleted", value))


async def table_updated(unit, value):
    voice_log.append(("table_updated", value))


data_table = Table(
    "Data table",
    headers=["Col A", "Col B"],
    rows=[["r1c1", "r1c2"], ["r2c1", "r2c2"], ["r3c1", "r3c2"]],
    changed=table_changed,
    delete=table_deleted,
    update=table_updated,
)

net_field = Graph("Net field", nodes=[Node("Alpha"), Node("Beta")], edges=[])


async def command_handler(unit, value):
    voice_log.append(("command", value))


command_field = Button("Go button", command_handler)


async def switch_changed(unit, value):
    voice_log.append(("switch_changed", value))


toggle_field = Switch("Toggle field", False, changed=switch_changed)

blocks = [
    Block(
        "Root",
        text_field,
        number_field,
        flag_field,
        color_field,
        readonly_field,
        data_table,
        net_field,
        command_field,
        toggle_field,
    )
]
