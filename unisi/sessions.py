# Copyright © 2024 UNISI Tech. All rights reserved.
"""The sessions of the app: which there are, when they were created and last used.

A session is exactly two things on disk -- `users/<id>.db` (its saved state, see
persist.py) and `<upload_dir>/<id>` (its working folder, `User.dir`) -- plus,
while a client is connected, its live `User`. Nothing records *when* a session
was created or last used, so the registry below does:

  * a session with a client connected right now is tracked in memory (`live`);
  * when its last client leaves it is written to `users/sessions.json`
    (`saved`) -- but only if it left something behind (saved state or a working
    folder) or was created from the admin panel. A session that leaves nothing
    is gone with its connection, exactly as before, so ordinary anonymous
    visits never pile up in the list;
  * sessions saved before the registry existed (a bare `users/<id>.db`) are
    listed too, with the dates read off the file -- where the platform keeps no
    creation time of a file (Linux), the creation date of such a session is
    unknown (None) rather than a wrong one.

A session that is not wanted any more can be put aside instead of deleted: `archive_session`
moves its two things into `archive/<id>/` (the saved state into `users/`, the working folder
into `upload_dir/` of it, and a `session.json` that says when it was archived and what dates it
had), `restore_session` moves them back to where they were, `archived_rows` lists the archive.

The admin panel (admin.py) is the only consumer of all this; `delete_session`, `archive_session`,
`restore_session` and `open_session` are what it does with it.
"""
import asyncio, json, logging, os, secrets, shutil, string, time
from .common import Unishare, Error
from .persist import Persist
from .utils import app_path, testdir
import config

REGISTRY_FILE = 'sessions.json'    # in users/, next to the <session>.db files
ARCHIVE_DIR = 'archive'            # in the app dir, next to users/: archive/<id>/{users,upload_dir,session.json}
ARCHIVE_INFO = 'session.json'      # written last: it is what makes archive/<id> an archived session
STATE_SUFFIXES = ('', '-wal', '-shm', '-journal')   # <id>.db and the files SQLite keeps beside it
DISCONNECT_TIMEOUT = 5             # seconds to wait for the clients of a deleted session to go away
FOLDER_SCAN_LIMIT = 20000          # files of a working folder counted to tell what is in it, no more


class SessionError(Exception):
    pass


def valid_id(session):
    """A session id is a file name (`users/<id>.db`, `<upload_dir>/<id>`), so it must not
    be able to name anything but a single file or folder."""
    return (isinstance(session, str) and 0 < len(session) <= 200 and session.isprintable()
            and session not in ('.', '..') and not set(session) & set('/\\'))


def session_dir(session):
    return app_path(config.upload_dir, session)


def archive_path(*parts):
    return app_path(ARCHIVE_DIR, *parts)


def folder_info(session):
    """What the working folder of the session (`<upload_dir>/<id>`, see `User.dir`) holds:
    None if there is no such folder, otherwise {'files': n, 'size': bytes, 'link': bool,
    'more': bool}. A link is not followed -- it stands for a folder somewhere else; only the
    first FOLDER_SCAN_LIMIT files are counted (`more`), so that a huge folder does not hold up
    the question that asks what to do with it."""
    folder = session_dir(session)
    if os.path.islink(folder):
        return dict(files=0, size=0, link=True, more=False)
    if not os.path.isdir(folder):
        return None
    files = size = 0
    for root, _, names in os.walk(folder):
        for name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if files >= FOLDER_SCAN_LIMIT:
                return dict(files=files, size=size, link=False, more=True)
    return dict(files=files, size=size, link=False, more=False)


def has_footprint(session):
    """True if the session has saved state or a working folder, i.e. outlives its connection."""
    return valid_id(session) and (Persist.exists(session) or os.path.isdir(session_dir(session)))


def file_times(path):
    """(created, last used) of a saved session, off its db file; created is None where the
    platform has no creation time of a file."""
    try:
        stat = os.stat(path)
    except OSError:
        return None, None
    used = stat.st_mtime
    try:    # a WAL database is written to its -wal file first
        used = max(used, os.stat(f'{path}-wal').st_mtime)
    except OSError:
        pass
    return getattr(stat, 'st_birthtime', None), used


def db_sessions():
    """ids of the sessions that have a saved state, whether the registry knows them or not"""
    folder = app_path('users')
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        session = name[:-3]
        if name.endswith('.db') and valid_id(session) and session != testdir:
            yield session


def new_session_id():
    from .users import User     # not at the top: users.py imports this module
    token = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    return f'{token}-{User.count}'     # the same look as the ids the server generates itself


def _time(value):
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class SessionRegistry:
    def __init__(self):
        self.live = {}      # id -> {'created': epoch | None, 'users': [User, ...]}, clients are connected
        self._saved = None
        self._path = None

    @property
    def saved(self):
        """id -> {'created': epoch | None, 'used': epoch | None}, as in users/sessions.json"""
        path = app_path('users', REGISTRY_FILE)
        if self._saved is None or path != self._path:    # the working dir of tests moves around
            self._path, self._saved = path, self._read(path)
        return self._saved

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding='utf-8') as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logging.warning(f'Session registry {path} is unreadable and is ignored: {e}')
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self):
        path = self._path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(f'{path}.tmp', 'w', encoding='utf-8') as file:
                json.dump(self._saved, file, ensure_ascii=False, indent=1)
            os.replace(f'{path}.tmp', path)    # atomic: a crash never leaves half a registry
        except OSError as e:
            logging.warning(f'Session registry {path} is not saved: {e}')

    def resumable(self, session):
        """True for a session nobody has to be connected to: it has a saved state or was
        created from the panel"""
        return session in self.saved or (valid_id(session) and Persist.exists(session))

    def knows(self, session):
        return session in self.live or self.resumable(session)

    def users(self, session):
        return list(self.live.get(session, {}).get('users', ()))

    def opened(self, session, user):
        """a client connected to the session"""
        live = self.live.get(session)
        if live is None:
            if session in self.saved:
                created = _time(self.saved[session].get('created'))
            elif valid_id(session) and Persist.exists(session):
                created = file_times(Persist.db_path_for(session))[0]
            else:
                created = time.time()
            live = self.live[session] = {'created': created, 'users': []}
        live['users'].append(user)

    def closed(self, session, user):
        """a client left the session; the last one to leave makes it a session of the past"""
        live = self.live.get(session)
        if live is None:        # forgotten (deleted) while it was connected
            return
        if user in live['users']:
            live['users'].remove(user)
        if live['users']:
            return
        del self.live[session]
        entry = self.saved.get(session)
        if entry is None and has_footprint(session):
            entry = self.saved[session] = {'created': live['created']}
        if entry is not None:
            entry['used'] = time.time()
            self._save()

    def create(self):
        """a new session of the panel: saved at once, unused until somebody loads it"""
        while True:
            session = new_session_id()
            if not self.knows(session):
                break
        self.saved[session] = {'created': time.time(), 'used': None}
        self._save()
        return session

    def forget(self, session):
        self.live.pop(session, None)
        if self.saved.pop(session, None) is not None:
            self._save()

    def restore(self, session, created, used):
        """a session brought back from the archive is listed again, with the dates it had"""
        self.saved[session] = {'created': created, 'used': used}
        self._save()

    def rows(self):
        """all the sessions, the active ones first, then the most recently used"""
        rows = {}
        for session, entry in self.saved.items():
            rows[session] = dict(id=session, created=_time(entry.get('created')), used=_time(entry.get('used')))
        for session in db_sessions():
            if session not in rows:
                created, used = file_times(Persist.db_path_for(session))
                rows[session] = dict(id=session, created=created, used=used)
        for session in {*self.live, *Unishare.sessions}:
            row = rows.setdefault(session, dict(id=session, created=None, used=None))
            if row['created'] is None and session not in self.saved:
                row['created'] = self.live.get(session, {}).get('created')
        for row in rows.values():
            row['active'] = row['id'] in self.live or row['id'] in Unishare.sessions
        return sorted(rows.values(), key=lambda r: (not r['active'], -((r['created'] if r['active'] else r['used'] or r['created']) or 0)))


session_registry = SessionRegistry()


def open_session(session, share=None, screen=None, register=True):
    """A new user of `session`. Without `share` it is the first one of the session, resumed
    from its saved state if there is one; `share` is a user it is one more view of (a live
    session of other clients, the one `config.mirror` mirrors). The session is registered, so
    a view of a live session (register=False) leaves `Unishare.sessions` to the first user."""
    from .users import User
    user = User.type(session, share, screen=screen)
    User.count += 1
    if register:
        Unishare.sessions[session] = user
    session_registry.opened(session, user)
    return user


async def _disconnect(user, reason):
    try:
        await user.send(Error(f'The session was {reason} by the administrator.'), persist=False)
    except Exception:
        pass
    try:
        await user.ws.close()
    except Exception:
        pass


def _check(session):
    """a session the administrator may do something with"""
    if not valid_id(session) or session == testdir:
        raise SessionError('Invalid session id')
    if not session_registry.knows(session) and session not in Unishare.sessions:
        raise SessionError('Unknown session')


async def _detach(session, keep, reason):
    """Take a session away from the running app, before its files are removed or moved: nobody
    of it saves anything any more (that would recreate the files), its database is closed, it is
    not one of `Unishare.sessions`, and its clients are told the `reason` and disconnected --
    but the users in `keep`: the administrator who deals with the session he works in only
    loses it and is to be moved elsewhere."""
    users = session_registry.users(session)
    if (root := Unishare.sessions.get(session)) and root not in users:
        users.append(root)
    for user in users:
        user._removed = True        # it must not save anything any more: that would recreate the files
        if db := getattr(user, 'db', None):
            user.db = None
            try:
                db.close()
            except Exception:
                pass
    if Unishare.sessions.get(session) in users:
        del Unishare.sessions[session]
    leaving = [user for user in users if user not in keep]
    if leaving:
        try:
            await asyncio.wait_for(asyncio.gather(*(_disconnect(user, reason) for user in leaving)), DISCONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            pass


async def delete_session(session, keep=()):
    """Remove a session as a whole: disconnect its clients, delete its saved state and its
    working folder, forget it. The users in `keep` are not disconnected -- the administrator
    who deletes the session he works in: they only lose it and are to be moved elsewhere."""
    _check(session)
    await _detach(session, keep, 'deleted')
    try:
        base = Persist.db_path_for(session)
        for suffix in STATE_SUFFIXES:
            try:
                os.remove(base + suffix)
            except FileNotFoundError:
                pass
        folder = session_dir(session)
        if os.path.islink(folder):
            os.unlink(folder)
        elif os.path.isdir(folder):
            shutil.rmtree(folder)
    except OSError as e:
        raise SessionError(f'The session files are not removed: {e.strerror or e}')
    session_registry.forget(session)


# ── the archive ───────────────────────────────────────────────────────────────
def _places(session):
    """[(where in the app, where in the archive)] of the two things a session is: the files of
    its saved state, and its working folder"""
    state = Persist.db_path_for(session)
    name = os.path.basename(state)
    places = [(state + suffix, archive_path(session, 'users', name + suffix)) for suffix in STATE_SUFFIXES]
    places.append((session_dir(session), archive_path(session, 'upload_dir')))
    return places


def _transfer(pairs, then=None):
    """Move each (source, destination) of `pairs`, then run `then`. If anything fails, what was
    moved is moved back: a session is never left half in one place and half in the other."""
    moved = []
    try:
        for source, destination in pairs:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(source, destination)
            moved.append((source, destination))
        if then:
            then()
    except BaseException:
        for source, destination in reversed(moved):
            try:
                shutil.move(destination, source)
            except OSError:
                logging.exception(f'{destination} is not moved back to {source}')
        raise


def _prune(folder):
    """remove `folder`, and the folders in it, as far as they are empty -- never a file"""
    for root, _, _ in os.walk(folder, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass


def _write_info(session, info):
    path = archive_path(session, ARCHIVE_INFO)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(f'{path}.tmp', 'w', encoding='utf-8') as file:
        json.dump(info, file, ensure_ascii=False, indent=1)
    os.replace(f'{path}.tmp', path)     # atomic, as the registry is: a crash never leaves half of it


def _read_info(session):
    try:
        with open(archive_path(session, ARCHIVE_INFO), encoding='utf-8') as file:
            info = json.load(file)
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _is_archived(session):
    return valid_id(session) and os.path.isfile(archive_path(session, ARCHIVE_INFO))


def _dates(session):
    """(created, last used) of a session as the panel lists them; one somebody works in is in use now"""
    row = next((row for row in session_registry.rows() if row['id'] == session), None)
    if row is None:
        return None, None
    return row['created'], time.time() if row['active'] else row['used']


def _store(session, info):
    """the files of the session into the archive, and its info last"""
    pairs = [(home, archived) for home, archived in _places(session) if os.path.lexists(home)]
    try:
        _transfer(pairs, lambda: _write_info(session, info))
    except BaseException:
        _prune(archive_path(session))       # the folders that were made for it
        raise


async def archive_session(session, keep=()):
    """Put a session aside instead of deleting it. Its saved state (`users/<id>.db` and what
    SQLite keeps beside it) goes to `archive/<id>/users/`, its working folder
    (`<upload_dir>/<id>`), if it has one, to `archive/<id>/upload_dir/`, and `archive/<id>/session.json`
    says when it was archived and what dates it had. For the app it is then as if it was deleted:
    its clients are disconnected (but the users in `keep`, see `delete_session`) and it is forgotten.
    A failure leaves everything where it was. `restore_session` brings it back."""
    _check(session)
    if os.path.lexists(archive_path(session)):
        raise SessionError(f'The archive has a session {session} already')
    created, used = _dates(session)
    await _detach(session, keep, 'archived')
    info = dict(session=session, created=created, used=used, archived=time.time())
    try:
        # off the event loop: a folder that goes to another disk is copied, which takes a while
        await asyncio.to_thread(_store, session, info)
    except OSError as e:
        raise SessionError(f'The session files are not moved: {e.strerror or e}')
    session_registry.forget(session)


def archived_rows():
    """the sessions of the archive, the most recently archived first"""
    try:
        names = os.listdir(archive_path())
    except OSError:
        return []
    rows = []
    for session in names:
        if not _is_archived(session):       # not a session of ours: some other folder
            continue
        info = _read_info(session)
        archived = _time(info.get('archived'))
        if archived is None:
            try:
                archived = os.stat(archive_path(session, ARCHIVE_INFO)).st_mtime
            except OSError:
                pass
        rows.append(dict(id=session, created=_time(info.get('created')), used=_time(info.get('used')), archived=archived))
    return sorted(rows, key=lambda r: -(r['archived'] or 0))


async def restore_session(session):
    """Bring an archived session back: its saved state to `users/`, its folder to
    `<upload_dir>/<id>` (the `upload_dir` of the config as it is now), and it is listed again with
    the dates it had. A session of the same id that is in the app is not replaced: it has to be
    deleted or archived first. A failure leaves everything where it was."""
    if not valid_id(session) or session == testdir:
        raise SessionError('Invalid session id')
    if not _is_archived(session):
        raise SessionError('The session is not in the archive')
    if session_registry.knows(session) or session in Unishare.sessions:
        raise SessionError(f'The session {session} exists already')
    pairs = [(archived, home) for home, archived in _places(session) if os.path.lexists(archived)]
    for _, home in pairs:
        if os.path.lexists(home):
            raise SessionError(f'{home} exists already')
    info = _read_info(session)
    try:
        await asyncio.to_thread(_transfer, pairs)
    except OSError as e:
        raise SessionError(f'The session files are not moved: {e.strerror or e}')
    session_registry.restore(session, _time(info.get('created')), _time(info.get('used')))
    try:
        os.remove(archive_path(session, ARCHIVE_INFO))
    except OSError:
        pass
    _prune(archive_path(session))


async def delete_archived(session):
    """remove a session of the archive for good"""
    if not _is_archived(session):
        raise SessionError('The session is not in the archive')
    try:
        await asyncio.to_thread(shutil.rmtree, archive_path(session))
    except OSError as e:
        raise SessionError(f'The archived session is not removed: {e.strerror or e}')
