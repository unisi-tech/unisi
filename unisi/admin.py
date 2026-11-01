# Copyright © 2024 UNISI Tech. All rights reserved.
"""The administrator's panel, built of UNISI units like anything else the app shows.

The panel is a Dialog with a Table of the sessions of the app: the session, when it was
created and when it was last used -- or `active` while somebody is connected to it.

  * a click on a session loads it: the connection of the administrator moves to it. A session
    that is live (some other client works in it) is joined, as `config.share` does; any other
    is resumed from its saved state;
  * `New` creates a session and loads it, `Refresh` reads the list again, `Close` closes it;
  * the basket of the table deletes the selected session (with the saved state and the files
    of it, after a confirmation). In the multiple mode of the table (its mode button) a click
    only marks a session, so several can be deleted at once without loading any.

It is shown by the `/admin` page of the web client (its connection is told so by `page=admin`
in the query of `/ws`, and serves no session: nothing of the app is loaded for it) and by the
admin button of the toolbar of any session, which sends the message `{"path": ["admin"]}`.
`admin = 'password'` in config.py of the app makes the panel ask for the password first;
without `admin` there is no password, `admin = False` turns the panel off.

The password is typed into a masked field (`Edit(type='password')`) and travels only as a
message of the websocket, like anything else the user enters: never in a URL, where it would
stay in the history of the browser and in the logs of every server on the way. What is typed
into such a field does not stay in `User.last_message` either (see `User.process_element`).
"""
import asyncio, hmac, time, traceback
from .common import ArgObject, Error, ReceivedMessage, TypeMessage, Unishare, Warning
from .containers import Dialog
from .tables import Table
from .units import Edit
from .users import User
from .sessions import SessionError, delete_session, open_session, session_registry
import config

ADMIN_PAGE = '/admin'           # the page of the web client that shows the panel
WRONG_PASSWORD_DELAY = 0.5      # seconds -- takes the speed out of guessing the password
HEADERS = ['Session', 'Created', 'Last used']


def admin_enabled():
    return getattr(config, 'admin', None) is not False


def admin_password():
    """config.admin: the password of the panel, None while it has none"""
    password = getattr(config, 'admin', None)
    return None if password is None or password is False or password == '' else str(password)


def password_ok(given):
    password = admin_password()
    return password is None or (isinstance(given, str) and hmac.compare_digest(given.encode(), password.encode()))


class AdminUser(User):
    """The user of an administrator's connection that serves no session: the page `/admin`
    before any session is loaded, or the connection of a session just deleted. It has none
    of the screens of the app (nothing of the app is loaded for it), saves nothing, and is
    not one of the sessions of the registry -- all it shows is the panel."""
    _removed = True     # see UserPersistMixin._persist_enabled
    console = True

    def __init__(self):
        last_user = User.last_user      # it is nobody's `last user`, which mirroring relies on
        super().__init__('')
        User.last_user = last_user
        self._screen = ArgObject(blocks=[], toolbar=[], menu=[], name='Administration', icon=None, order=0,
            header=f'{config.appname}: administration', image=config.image, lang=config.lang,
            voice=False, reload=False, persist=False, prepare=None, admin=True, type='screen')

    def load_lazy(self, screen=None):
        pass

    def monitor(self, session, share=None):
        pass

    @property
    def screen(self):
        return self._screen

    async def delete(self):
        pass


class AdminDialog(Dialog):
    """a window of the panel. Only the administrator has it: what is sent while one is open is
    not reflected to the other users of a shared session (server.py), nor is its closing."""
    private = True


def is_admin_message(user, message):
    dialog = user.active_dialog
    if message.admin_type:
        return True
    path = message.path
    return isinstance(dialog, AdminDialog) and isinstance(path, list) and bool(path) and path[-1] == dialog.name


async def open_for(user):
    """what the page `/admin` shows at once: the same as the admin button of the toolbar"""
    return await user.result4message(ReceivedMessage(dict(path=['admin'], event='changed', value=None)))


async def open_panel(user):
    """the panel -- or the password to be entered first -- as the answer to the message
    `{"path": ["admin"]}`"""
    if not admin_enabled():
        return Error('The admin panel is turned off.')
    if admin_password() is not None and not user.admin_ok:
        return ask_password(user)
    return panel(user)


def ask_password(user):
    password = Edit('Password', '', type='password', focus=True)

    async def check(dialog, button):
        if button != 'Enter':
            return None
        if password_ok(password.value):
            user.admin_ok = True
            return panel(user)
        await asyncio.sleep(WRONG_PASSWORD_DELAY)
        await user.send(Warning('Wrong password'), persist=False)
        return ask_password(user)

    return AdminDialog('Administrator password', check, password, commands=['Enter', 'Cancel'], icon='lock')


def stamp(moment):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(moment)) if moment else None


def current_session(user):
    return None if getattr(user, 'console', False) else user.session


async def close_panel(user):
    if user.active_dialog is not None:
        user.active_dialog = None
        await user.send(TypeMessage('action', 'close'), persist=False)


async def load(user, session):
    """moves the connection of `user` to `session`"""
    try:
        root = Unishare.sessions.get(session)
        loaded = open_session(session, root, register=False) if root else open_session(session)
    except Exception as e:
        user.log(traceback.format_exc())
        return Error(f'The session {session} is not loaded: {e}')
    await close_panel(user)
    await user.switch(loaded)


def panel(user):
    rows = session_registry.rows()
    ids = [row['id'] for row in rows]
    current = current_session(user)

    async def choose(table, value):
        if not isinstance(value, int) or isinstance(value, bool):
            return None     # nothing is chosen, or the sessions are marked to be deleted
        if not 0 <= value < len(ids):
            return Error('The session list is out of date. Refresh it!')
        if ids[value] == current:
            return await close_panel(user)
        return await load(user, ids[value])

    async def remove(table, value):
        marked = value if isinstance(value, list) else [value]
        sessions = [ids[i] for i in marked if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(ids)]
        return confirm(user, sessions) if sessions else panel(user)

    async def command(dialog, button):
        match button:
            case 'New':
                return await load(user, session_registry.create())
            case 'Refresh':
                return panel(user)

    table = Table('Sessions', ids.index(current) if current in ids else None, choose, delete=remove, edit=False,
        headers=HEADERS, rows=[[row['id'], stamp(row['created']), 'active' if row['active'] else stamp(row['used'])]
            for row in rows])
    return AdminDialog('Administration', command, table, commands=['Close', 'New', 'Refresh'], icon='admin_panel_settings')


def confirm(user, sessions):
    async def answer(dialog, button):
        return await delete(user, sessions) if button == 'Delete' else panel(user)

    what = f'the session {sessions[0]}' if len(sessions) == 1 else f'{len(sessions)} sessions'
    return AdminDialog(f'Delete {what} with its data and files?', answer, commands=['Cancel', 'Delete'], icon='delete_forever')


async def delete(user, sessions):
    failed = []
    for session in sessions:
        try:
            await delete_session(session, keep=[user])
        except SessionError as e:
            failed.append(f'{session}: {e}')
    if failed:
        await user.send(Error('\n'.join(failed)), persist=False)
    if current_session(user) in sessions and getattr(user, '_removed', False):
        # the session the administrator works in is gone: the connection is left with the panel only
        console = AdminUser()
        await user.switch(console)
        console.active_dialog = dialog = panel(console)
        await console.send(dialog)
        return None
    return panel(user)
