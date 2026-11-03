# Copyright © 2024 UNISI Tech. All rights reserved.
"""The administrator's panel, built of UNISI units like anything else the app shows.

The panel is a Dialog with a Table of the sessions of the app: the session, when it was
created and when it was last used -- or `active` while somebody is connected to it.

  * a click on a session only selects it, and `Go to` loads the selected one: the connection of
    the administrator moves to it. A session that is live (some other client works in it) is
    joined, as `config.share` does; any other is resumed from its saved state;
  * `New` creates a session and goes to it, `Refresh` reads the list again (the selection
    stays), `Close` closes the panel;
  * the basket of the table asks what to do with the selected session: `Delete` removes it
    for good (its saved state and its working folder, `<upload_dir>/<id>`), `Archive` puts it
    aside, into `archive/<id>/`, from where it can be brought back, `Cancel` leaves it be.
    In the multiple mode of the table (its mode button) several sessions can be marked and
    dealt with at once;
  * the switch `Archive` shows the archive instead of the sessions of the app. There `Restore`
    brings the selected sessions back to where they were (the saved state to `users/`, the
    folder to `<upload_dir>/<id>`), and the basket removes them from the archive for good.

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
from .common import ArgObject, Error, Info, ReceivedMessage, TypeMessage, Unishare, Warning
from .containers import Dialog
from .tables import Table
from .units import Edit, Switch, Text
from .users import User
from .sessions import (SessionError, archive_session, archived_rows, delete_archived, delete_session, folder_info,
                       open_session, restore_session, session_registry)
import config

ADMIN_PAGE = '/admin'           # the page of the web client that shows the panel
WRONG_PASSWORD_DELAY = 0.5      # seconds -- takes the speed out of guessing the password
HEADERS = ['Session', 'Created', 'Last used']
ARCHIVE_HEADERS = ['Session', 'Last used', 'Archived']
STALE = 'The session list is out of date. Refresh it!'
NOTHING = object()              # `selected` of panel(): no session is marked in the list


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


def panel(user, selected=None, archive=False):
    """The panel. `selected` is the session marked in the list: the one `user` is in, if none is
    given (NOTHING: none is marked). `archive` shows the archive instead of the sessions of the app."""
    rows = archived_rows() if archive else session_registry.rows()
    ids = [row['id'] for row in rows]
    current = None if archive else current_session(user)
    marked = selected if selected in ids else None if selected is NOTHING else current

    def chosen():
        """the sessions the table has selected -- None if it was selected in a list that is out of date"""
        value = table.value
        indices = value if isinstance(value, list | tuple) else [] if value is None else [value]
        if all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(ids) for i in indices):
            return [ids[i] for i in indices]

    def picked(what='go to'):
        """the one session to `what`: (its id, None) -- or (what the next panel marks instead, what
        is wrong with the selection)"""
        sessions = chosen()
        if sessions is None:
            return None, Error(STALE)
        if not sessions:
            return NOTHING, Warning(f'Select the session to {what}.')
        if len(sessions) > 1:
            return None, Warning(f'Select one session to {what}.')
        return sessions[0], None

    async def again(problem, session=None):
        """what went wrong is told, and the panel is there for another try: a button closes its window"""
        await user.send(problem, persist=False)
        return panel(user, session, archive)

    async def remove(table, value):
        marked = value if isinstance(value, list) else [value]
        sessions = [ids[i] for i in marked if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(ids)]
        if not sessions:
            return panel(user, archive=archive)
        return confirm_purge(user, sessions) if archive else confirm(user, sessions)

    async def toggle(switch, value):
        return panel(user, archive=bool(value))

    async def command(dialog, button):
        match button:
            case 'Go to':
                session, problem = picked()
                if problem is None and session != current:      # to the session the user is in is nowhere to go
                    problem = await load(user, session)
                return await again(problem, session) if problem else None
            case 'New':
                problem = await load(user, session_registry.create())
                return await again(problem) if problem else None
            case 'Restore':
                sessions = chosen()
                if sessions is None:
                    return await again(Error(STALE))
                if not sessions:
                    return await again(Warning('Select the sessions to restore.'), NOTHING)
                return await restore_sessions(user, sessions)
            case 'Refresh':
                return panel(user, picked()[0], archive)

    if archive:
        table = Table('Archived sessions', ids.index(marked) if marked in ids else None, delete=remove, edit=False,
            headers=ARCHIVE_HEADERS, rows=[[row['id'], stamp(row['used']), stamp(row['archived'])] for row in rows])
        commands = ['Close', 'Restore', 'Refresh']
    else:
        table = Table('Sessions', ids.index(marked) if marked in ids else None, delete=remove, edit=False,
            headers=HEADERS, rows=[[row['id'], stamp(row['created']), 'active' if row['active'] else stamp(row['used'])]
                for row in rows])
        commands = ['Close', 'Go to', 'New', 'Refresh']
    return AdminDialog('Administration', command, Switch('Archive', archive, toggle), table, commands=commands,
        icon='admin_panel_settings')


def size_of(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def folders(sessions):
    """What `upload_dir` holds of the sessions: their working folders, `<upload_dir>/<id>`. A line
    for the question what to do with the sessions -- there is none if they have no folders."""
    found = [info for session in sessions if (info := folder_info(session))]
    if not found:
        return []
    files, size = sum(info['files'] for info in found), sum(info['size'] for info in found)
    more = '+' if any(info['more'] for info in found) else ''
    if len(sessions) == 1 and found[0]['link']:
        what = 'a link'
    elif files:
        what = f'{files}{more} file{"" if files == 1 and not more else "s"}, {size_of(size)}'
    else:
        what = 'empty'
    if len(sessions) == 1:
        return [Text(f'Folder {config.upload_dir}/{sessions[0]}: {what}')]
    return [Text(f'{len(found)} of {len(sessions)} sessions have a folder in {config.upload_dir}: {what}')]


def confirm(user, sessions):
    """What to do with the sessions the basket was pressed for: remove them for good, or put them
    aside into the archive. `Cancel` is the primary button -- the one Enter presses."""
    async def answer(dialog, button):
        match button:
            case 'Delete':
                return await delete_sessions(user, sessions)
            case 'Archive':
                return await archive_sessions(user, sessions)
        return panel(user)

    what, them = (f'the session {sessions[0]}', 'it') if len(sessions) == 1 else (f'{len(sessions)} sessions', 'them')
    return AdminDialog(f'Delete {what} completely or archive {them}?', answer, *folders(sessions),
        commands=['Cancel', 'Archive', 'Delete'], icon='archive')


def confirm_purge(user, sessions):
    """the basket of the archive: the sessions are removed from it for good"""
    async def answer(dialog, button):
        if button == 'Delete':
            failed = []
            for session in sessions:
                try:
                    await delete_archived(session)
                except SessionError as e:
                    failed.append(f'{session}: {e}')
            if failed:
                await user.send(Error('\n'.join(failed)), persist=False)
        return panel(user, archive=True)

    what = f'the archived session {sessions[0]}' if len(sessions) == 1 else f'{len(sessions)} archived sessions'
    return AdminDialog(f'Delete {what} for good?', answer, commands=['Cancel', 'Delete'], icon='delete_forever')


async def gone(user, sessions):
    """the panel, once sessions are out of the app -- or the console of the administrator,
    if the session he works in is one of them"""
    if current_session(user) in sessions and getattr(user, '_removed', False):
        console = AdminUser()
        await user.switch(console)
        console.active_dialog = dialog = panel(console)
        await console.send(dialog)
        return None
    return panel(user)


async def delete_sessions(user, sessions):
    failed = []
    for session in sessions:
        try:
            await delete_session(session, keep=[user])
        except SessionError as e:
            failed.append(f'{session}: {e}')
    if failed:
        await user.send(Error('\n'.join(failed)), persist=False)
    return await gone(user, sessions)


async def archive_sessions(user, sessions):
    done, failed = [], []
    for session in sessions:
        try:
            await archive_session(session, keep=[user])
            done.append(session)
        except SessionError as e:
            failed.append(f'{session}: {e}')
    if done:
        await user.send(Info(f'Archived: {", ".join(done)}'), persist=False)
    if failed:
        await user.send(Error('\n'.join(failed)), persist=False)
    return await gone(user, sessions)


async def restore_sessions(user, sessions):
    done, failed = [], []
    for session in sessions:
        try:
            await restore_session(session)
            done.append(session)
        except SessionError as e:
            failed.append(f'{session}: {e}')
    if done:
        await user.send(Info(f'Restored: {", ".join(done)}'), persist=False)
    if failed:
        await user.send(Error('\n'.join(failed)), persist=False)
    return panel(user, archive=True)
