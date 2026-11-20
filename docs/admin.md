---
title: Administration
nav_order: 10
---

# Administration
{: .no_toc }

1. TOC
{:toc}

## Users and roles

The server has local user accounts. There is no self-registration and no
password reset by email: site admins manage all accounts.
**Avoid password reuse**. I have attempted to follow best-practices for
storing hashed passwords, but you should assume security is minimal at best.

Each user has one role, and each role can do everything the ones before it can:

| Role | Can |
|------|-----|
| `viewer` | read everything. Only needed when anonymous viewing is off |
| `editor` | edit documents; create, edit, merge and delete branches; import |
| `admin` | also create tags (read-only snapshots), and clear version locks |
| `site_admin` | also change the site settings, manage users and run maintenance, on the **Admin** pages |

Anyone can view the models without logging in, unless **allow anon view** is
turned off in the site settings. Users can hange their own password on their
**profile** page.

### Users

**Admin → Users** lists the accounts. **New user** asks for a name, role and
password. Editing a user can change their role, deactivate them, or set a new
password; a new password or deactivation ends their sessions. The last active
site admin can't be demoted or deactivated.

## Site settings

**Admin → Site settings**:

- **Organization name, URL and logo**: the logo (an image under 1 MB) is shown at
  the right of the navigation bar, linked to the URL.
- **Allow anon view**: whether people who aren't logged in can view models.

## Maintenance

**Admin → Maintenance** shows the size of each collection and version, and has:

- **Clear calculation cache**: deletes all stored calculated results. They are
  recalculated when next viewed.
- **Rebuild source terms**: deletes and regenerates all SourceTerms of a
  branch. SourceTerms are normally kept up to date automatically; use this if
  they look wrong.
- **Locks**: lists the locked versions and who locked them and why. Locks
  expire a few minutes after their operation last renewed them, so only
  locks left by failed operations stay (those without an expiry always do).
  **Clear expired locks** removes the ones that have run out; **Clear**
  removes any single lock, which is only safe if its operation is no longer
  running.
- **Delete orphans**: removes documents that no version uses, and SourceTerms
  that refer to missing documents. Editing a document that several versions
  share first copies it under a temporary tag; if that is interrupted, the
  copy is listed here and this restores it to its version, or discards it if
  the version still has the document. Copies younger than a minute are left
  alone.
- **Jobs**: lists the [background jobs](#background-jobs) with their status,
  progress and who started them. **Cancel** removes a queued job, or asks a
  running one to stop at its next safe point. **Abandon** gives up on a running
  job that is stuck, so that it no longer holds up the jobs behind it; it
  doesn't stop whatever is running it, and doesn't release the version's lock
  (see Locks). **Clear finished jobs** removes the finished ones, and their
  files; they are removed after `JOBS_KEEP_DAYS` anyway. The same is done by
  [`bgexplorer-jobs`](cli.html#bgexplorer-jobs).

## Spectrum binning

Spectra of the same name share their bins, which the first one sets.
Administrators reset them from a version's settings page, once the spectra are
deleted; see [spectrum binning](settings.html#spectrum-binning).

## Background jobs

Edits that can take minutes on a large model (changing a version's settings,
rebuilding source terms, merging, importing, exporting and deleting a version)
run as background jobs. After submitting one, the browser is taken to a page
that follows its progress, and then to where the edit was made. If the job
finishes within `JOBS_GRACE_SECONDS`, the page isn't shown at all.

Jobs of one version run one at a time in the order they were submitted, and the
ones that rewrite the whole version (rebuild, merge, import, delete) wait for
the others to finish. Jobs of different versions run in parallel, up to
`JOBS_WORKERS` at once.

While a version has jobs that edit it, its pages show a banner saying that
the figures may not include the edits yet, and are calculated from the results
as they were. API responses have the header `X-Bgexplorer-Stale`, see the
[API](api.html#jobs).

A running job renews its lease every few seconds. If the process running it dies,
the lease runs out after `JOBS_LEASE_SECONDS` and the job is marked failed (or
run again, for the jobs that are safe to repeat) the next time a worker looks.

How the jobs are run depends on `JOBS_MODE`:

- `thread`: in threads of the web server's processes. They share the memory
  and the cache of calculated results with the pages, but compete with them
  for the CPU.
- `process`: in separate worker processes started by each web server process.
  They don't slow down the web threads, but they start with no cache and use
  memory of their own.
- `eager`: during the request, with no pool; the setting of the tests.

## Configuration

The server is configured with environment variables and a configuration file.
Any Flask setting can be
given as `FLASK_<NAME>`; values are parsed as JSON where possible, so
`FLASK_LOGIN_DISABLED=true` is a boolean.

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLASK_MONGODB_URI` | `mongodb://127.0.0.1:27017/bgexplorer3_testing` (Docker: `mongodb://mongo:27017/bgexplorer`) | the database |
| `FLASK_SECRET_KEY` | generated | protects login sessions, see below |
| `FLASK_SESSION_COOKIE_SECURE` | `false` | set `true` when users reach the server over https |
| `FLASK_LOGIN_DISABLED` | `false` | give everyone every role, e.g. for a server only you can reach |
| `GUNICORN_CMD_ARGS` | `--workers 2 --threads 4 --timeout 120 --access-logfile -` | gunicorn's options, in Docker |
| `BGEXPLORER_PORT` | `8000` | the port Docker Compose publishes |
| `FLASK_JOBS_MODE` | `thread` | how [background jobs](#background-jobs) run: `thread`, `process` or `eager` |
| `FLASK_JOBS_WORKERS` | CPU count, up to 4 | jobs run at once in each server process |
| `FLASK_JOBS_GRACE_SECONDS` | `2.5` | how long a web request waits for its job before the browser is sent to the status page |
| `FLASK_JOBS_API_WAIT_SECONDS` | `100` | how long an API request waits for its job before answering `202` |
| `FLASK_JOBS_LEASE_SECONDS` | `60` | a running job whose lease wasn't renewed for this long is considered dead |
| `FLASK_JOBS_POLL_SECONDS` | `1` | how often idle workers look for new jobs |
| `FLASK_JOBS_PROGRESS_INTERVAL` | `0.5` | the least time between writes of a job's progress |
| `FLASK_JOBS_KEEP_DAYS` | `7` | how long finished jobs are kept |
| `FLASK_JOBS_SHUTDOWN_SECONDS` | `20` | how long to let running jobs finish when the server stops |
| `FLASK_JOBS_FILES_DIR` | `bgexplorer-jobs` in the temporary directory | where uploaded and exported version files wait for their jobs; every server process and worker must reach it, so with several containers it must be a shared volume |

Settings can also be put in a Python file named by `BGEXPLORER_CONFIG`.

### The secret key

The `SECRET_KEY` signs login sessions: anyone who knows it can log in as any
user. If none is configured, the server generates one the first time it starts
and keeps it in the database's `server_secrets` collection. It then stays the
same across restarts and is shared by every worker.

To end everyone's sessions, delete that document (or change the configured
key) and restart.

### https

Run Background Explorer behind a reverse proxy, such as nginx or Caddy, that
handles TLS, and set `FLASK_SESSION_COOKIE_SECURE=true`.

## Backups

- Export important versions to files, see [Versions](versions.html#export-and-import).
- Back up the whole database with `mongodump`, which also keeps the users and
  site settings. With Docker Compose:

  ```sh
  docker compose exec mongo mongodump --archive --gzip > bgexplorer.archive.gz
  ```
