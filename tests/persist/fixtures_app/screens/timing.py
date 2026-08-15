from unisi import *

name = "Timing"
order = 2

status = Edit("Status", "idle", persist=True)


async def run_task(button, _):
    status.value = "step1"
    await user.progress("33%")
    status.value = "step2"
    await user.progress("66%")
    status.value = "step3"
    return None


run_button = Button("Run task", run_task)


async def dialog_callback(dialog, button_name):
    status.value = f"dialog:{button_name}"
    return None


async def open_dialog(button, _):
    return Dialog("Confirm?", dialog_callback)


dialog_button = Button("Open dialog", open_dialog)

blocks = [Block("Root", status, run_button, dialog_button)]
