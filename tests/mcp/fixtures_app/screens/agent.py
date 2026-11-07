from unisi import *

name = "Agent"
order = 0

note = Edit("Note", "")
count = Edit("Count", 0)
log = TextArea("Log", "")
rows = Table("Rows", None, headers = ["n"], rows = [[1], [2]])


def bump(_, __):
    count.value += 1
    return Info("bumped")


def answered(_, command):
    log.value += f"{command}\n"


def ask(_, __):
    note.value = "asked"            # changed before the dialog: must reach the page ahead of it
    return Dialog("Sure?", answered, commands = ["Yes", "No"])


def note_text(prefix: str = "") -> dict:
    """Текст заметки (с приставкой)."""
    return {"note": prefix + note.value}


async def set_note(text: str):
    """Записать заметку."""
    note.value = text
    return "ok"


def boom():
    """Падает."""
    raise RuntimeError("boom")


tools = [note_text, set_note, boom]
blocks = [Block("Main", note, count, log, rows, Button("Bump", bump), Button("Ask", ask))]
