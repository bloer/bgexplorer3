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
- **Delete orphans**: removes documents that no version uses, and SourceTerms
  that refer to missing documents.

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
