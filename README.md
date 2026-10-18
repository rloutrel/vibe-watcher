# vibe-watcher

A self-hosted background watcher for local git checkouts. The watcher process
polls your configured folders on their own intervals, detects when the remote
has commits your local copy doesn't have, sends a desktop notification, and
records snapshots/events that the bundled web UI displays — including a
watcher heartbeat indicator that flags a stale or stopped watcher.

- **Watcher**: long-lived process via a systemd service — `python manage.py watch_folders` (single process, no Celery, no cron).
- **Notifications**: D-Bus session bus (`org.freedesktop.Notifications` via `notify-send`), with notification-id reuse so repeat events update the same desktop bubble instead of stacking.
- **Web UI**: folder list with latest states, recent events (from the database, not log files), and a watcher status indicator.

## Components

| Piece | Where |
|---|---|
| Watcher loop | `watcher/scanner.py` + `watcher/management/commands/watch_folders.py` |
| Git interactions | `watcher/git.py` (subprocess-based, mockable) |
| Notifications | `watcher/notify.py` (dbus / none backends) |
| Web UI | `watcher/views.py`, `watcher/templates/watcher/` |
| Systemd packaging | `packaging/systemd/` |
| Compose setup | `compose/docker-compose.yml` |
| Tests | `watcher/tests/` |

## Quickstart (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver          # terminal 1: web UI at http://127.0.0.1:8000
python manage.py watch_folders      # terminal 2: watcher loop
```

Add folders in the UI (`/` → *Add folder*) or via `/admin/`.

**Quick add**: type just the path (with `~` supported — it's expanded
server-side against the watcher user's home) and leave the other fields
empty. When the path field loses focus, the form autodetects the repo:
name (from the directory), checked-out branch, remote URL, and available
branches. If the path isn't a git repo, you can still fill the fields
manually — autodetection reports errors but never blocks saving. The name
is also derived from the path server-side on submit, so posting a bare path
works too.

Each folder
can optionally select a specific **branch to watch**: the branch picker lists
everything available in the repo (local and remote, via `git branch -a`);
leave it empty to watch the currently checked-out branch.

## Compose setup

```bash
cd compose
docker compose up --build
```

Runs three services: `web` (Django dev server on :8000), `watcher`
(`manage.py watch_folders`), and `db` (PostgreSQL 16). The watcher mounts your
home directory read-only at `/home/hostuser` so you can point folders at
`/home/hostuser/code/...`. Desktop notifications are disabled by default in
compose (`WATCHER_NOTIFY_BACKEND=none`) because containers have no session bus.

## Configuration (12-factor env vars)

All configuration is environment-driven, both for compose and the systemd
`EnvironmentFile`. See `packaging/systemd/env.example` for the full annotated list.

| Variable | Default | Meaning |
|---|---|---|
| `VIBE_WATCHER_SECRET_KEY` | dev value | Django secret key — set in production |
| `VIBE_WATCHER_DEBUG` | `1` | Django debug mode |
| `VIBE_WATCHER_ALLOWED_HOSTS` | `*` | Comma-separated hosts |
| `VIBE_WATCHER_DB` | sqlite | `postgres` to use the PostgreSQL block |
| `VIBE_WATCHER_DB_PATH` | `./db.sqlite3` | SQLite file location |
| `POSTGRES_*` | see settings | PostgreSQL connection block |
| `WATCHER_NOTIFY_BACKEND` | `dbus` | `dbus` (notify-send) or `none` |
| `WATCHER_NOTIFY_APP_NAME` | `vibe-watcher` | `-a` app name passed to notify-send |
| `WATCHER_NOTIFY_SEND_ARGS` | *(empty)* | Extra args passed verbatim to notify-send (e.g. `--icon=git --transient`) |
| `VIBE_WATCHER_FETCH_BEFORE_SCAN` | `1` | Run `git fetch --all` before each scan so remote changes are actually detected |
| `VIBE_WATCHER_CONFIG_PATH` | unset | On-disk config copy the service rewrites on every folder change and restores from at startup when the DB is empty |
| `WATCHER_HEARTBEAT_STALE_AFTER` | `180` | Seconds after which the UI flags the watcher as stale |
| `WATCHER_DEFAULT_POLL_INTERVAL` | `30` | Default per-folder poll interval |
| `WATCHER_GIT_TIMEOUT` | `120` | Max seconds for one git invocation |
| `WATCHER_LOG_LEVEL` | `INFO` | Log level for the JSON stdout logger |

## Installing as a systemd service

The watcher is designed to run as a long-lived foreground process managed by
systemd — not cron, not a scheduler thread. Two unit files are provided:

- `packaging/systemd/vibe-watcher.service` — system mode
- `packaging/systemd/vibe-watcher-user.service` — user mode

Both use `Type=simple`, `ExecStart=... manage.py watch_folders`,
`Restart=on-failure`, `RestartSec=10`, an `EnvironmentFile`, and depend on
`network-online.target` (`Wants=`/`After=`).

### Which mode should I use?

**User mode (recommended for desktop notifications).** A user service runs
inside your login session, so `notify-send` can reach the D-Bus *session* bus
directly — no extra configuration. This is the reliable option for
notifications.

**System mode.** Use it when the watcher runs on a headless box or must keep
running regardless of logins. Note the D-Bus caveat below.

### User mode install

```bash
git clone https://github.com/rloutrel/vibe-watcher ~/vibe-watcher
cd ~/vibe-watcher
./packaging/systemd/install.sh --user
```

The script copies the unit to `~/.config/systemd/user/vibe-watcher.service`,
writes `~/.config/vibe-watcher/env` (edit it), runs `systemctl --user
daemon-reload` and `systemctl --user enable --now vibe-watcher`, and enables
`loginctl enable-linger` so the watcher survives logout.

Manual equivalent:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/vibe-watcher-user.service ~/.config/systemd/user/vibe-watcher.service
systemctl --user daemon-reload
systemctl --user enable --now vibe-watcher.service
```

### System mode install

```bash
sudo ./packaging/systemd/install.sh --system            # installs to /opt/vibe-watcher
```

The script copies the project to `/opt/vibe-watcher`, creates the
`vibe-watcher` system user, writes `/etc/vibe-watcher/env`, installs the unit
to `/etc/systemd/system/vibe-watcher.service`, runs `systemctl daemon-reload`
and `systemctl enable --now vibe-watcher`.

Manual equivalent:

```bash
sudo install -m 0644 packaging/systemd/vibe-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vibe-watcher
```

`User=`/`Group=` in the unit default to `vibe-watcher`; change them to a user
that can read the repositories you're watching.

### D-Bus notifications in system mode (important)

Desktop notifications go to the D-Bus **session** bus, which a system service
does not have. Options, in order of reliability:

1. **Use user mode instead** — this is the reliable option and the reason the
   user unit exists.
2. If you must run in system mode, export the session bus address of your
   logged-in desktop user into the service environment (`/etc/vibe-watcher/env`):

   ```bash
   # find it in your desktop session:
   echo "$DBUS_SESSION_BUS_ADDRESS"
   # typically: unix:path=/run/user/1000/bus
   ```

   ```ini
   DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
   ```

   Caveats: notifications only appear while that user session is active, and
   the service user needs permission on that socket (it's `0600` and owned by
   the session user by default). You may need to relax socket permissions or
   run the service as the desktop user (`User=youruser`), which is why user
   mode is recommended.
3. Set `WATCHER_NOTIFY_BACKEND=none` on headless machines; events still show
   in the web UI.

## Change detection

The watcher runs ``git fetch --all`` before every scan, so remote-tracking
refs (`origin/main` & co) always reflect the actual state of the remote —
without a fetch, local refs only move when *you* run git fetch, and remote
changes would be missed. Set `VIBE_WATCHER_FETCH_BEFORE_SCAN=0` to disable
(e.g. for huge repos where you prefer polling local refs only); fetch
failures (offline) degrade gracefully to the last known refs.

Each folder row has a **⟳ Check now** button that scans that folder
immediately instead of waiting for its poll interval.

### Automatic pull and restart

Each folder has two extra options:

- **Auto-pull** (`auto_pull`): when the remote changes, the watcher runs
  `git pull --ff-only` automatically. Fast-forward only — if your local
  branch diverged or the tree is dirty, the pull fails safely (nothing is
  merged or lost) and the notification tells you to intervene manually.
- **Post-pull command** (`post_pull_command`): a shell command executed in
  the folder after a successful pull — typically
  `docker compose up --build -d` to rebuild and restart the stack. Output
  and errors are captured in the event feed.

Workflow: remote push, watcher fetches, detects change, pulls (ff-only),
runs the post-pull command, and the desktop notification confirms
"Pulled and restarted."


## Folder management

- **ON/OFF switch**: each folder row has a toggle — green = watching, grey =
  paused. Flipping it enables/disables the folder without deleting it.
- **Icon actions**: ✏️ edits, 🗑️ deletes (with a confirmation prompt).
- **Automatic local persistence**: the service keeps a JSON copy of the
  folder configuration on disk. Set `VIBE_WATCHER_CONFIG_PATH` (see the
  env example; e.g. `/var/lib/vibe-watcher/watcher-config.json`) and every
  folder change rewrites it automatically. On startup, if the database has
  no folders but the file exists, the configuration is recovered from it —
  so a wiped database self-heals on the next `watch_folders` run.
- **Manual backup / restore**: *Export config* downloads `vibe-watcher-config.json`
  with all folders (path, branch, interval, enabled, url); *Import config*
  re-uploads it — folders are matched by path: existing ones are updated,
  missing ones are recreated. Use it to recover your setup after a database
  loss or to move the watcher to another machine. Snapshots and events are
  not part of the backup.

## Branch selection

Each folder has an optional **Branch** field:

- Empty (default) — the watcher observes the currently checked-out branch and
  its upstream.
- A local branch name (e.g. `main`, `dev`) — the watcher uses that branch's
  upstream if configured, otherwise the branch tip.
- A remote-tracking ref (e.g. `origin/main`) — the watcher compares the local
  HEAD against that remote tip; pick this if you want to be notified when the
  remote moves even if you don't have the branch checked out.

The add/edit form renders the Branch field as a **dropdown** (`<select>`)
populated from `/folders/<id>/branches/` (saved folders) or
`/folders/branches/?path=...` (unsaved paths): the current branch is marked,
remoteless branches appear disabled with a `— no remote` marker, and the
first option is the empty default (checked-out branch). A saved branch that
no longer exists in the repo is kept as `(missing from repo)` so it stays
visible and editable.

## Notification id reuse

- D-Bus notifications use `notify-send --print-id` and `-r <id>` so a repeated
  event for the same commit **replaces** the existing desktop bubble rather
  than stacking duplicates.
- The mapping from a stable notification key (`f<folder_id>:<commit>`) to the
  numeric D-Bus notification id is persisted in `NotificationState` and reused
  within a `NOTIFICATION_REUSE_WINDOW_MINUTES` window (default 60 minutes).
- Independently, `NotifiedCommit` records which (folder, commit) pairs have
  already been notified so a commit is never notified twice; entries older
  than 30 days are pruned each cycle.

## Watcher status (heartbeat)

The watcher writes a heartbeat row (`WatcherStatus`) after every loop pass
(and on each scan in `--once` mode). The web UI shows a badge:

- **Running** — heartbeat is fresh (within `WATCHER_HEARTBEAT_STALE_AFTER`).
- **Stale heartbeat** — watcher appears stopped or hung.
- **Never run** — no heartbeat recorded yet.

## Logs

The watcher logs structured JSON to stdout, which journald captures:
`journalctl -u vibe-watcher -f` (or `journalctl --user -u vibe-watcher -f` in
user mode). The web UI's *Recent events* feed reads from the database
(`WatcherEvent`), not from log files.

## Tests

```bash
pip install -r requirements.txt
python manage.py test watcher
```

All git interactions and all notification subprocess calls are mocked; no
real `git` or `notify-send` binaries are needed.

## License

See [LICENSE](LICENSE).
