---
title: Command line
nav_order: 11
---

# Command line tools
{: .no_toc }

1. TOC
{:toc}

Three commands are installed with the package. They only connect to the
database, given by `--uri` or the `FLASK_MONGODB_URI` environment variable, so
they don't need the web server's configuration. With Docker Compose, run them
in the web container, e.g. `docker compose exec web bgexplorer-users ...`.

## bgexplorer-users

Manage user accounts.

```sh
bgexplorer-users [--uri URI] create NAME [--role viewer|editor|admin|site_admin]
bgexplorer-users [--uri URI] set-password NAME
```

- `create` makes an account; the role defaults to `viewer`. It asks for the
  password, or takes `--password`.
- `set-password` sets a user's password, e.g. if they forgot it, and logs them
  out.

`python -m bgexplorer.cli` also works when the package isn't installed.

## bgexplorer-versions

List, export and import versions.

```sh
bgexplorer-versions [--uri URI] list
bgexplorer-versions [--uri URI] export NAME [-o FILE]
bgexplorer-versions [--uri URI] import FILE --name NEW [--tag] [--description TEXT]
```

- `list` prints each version's name, type (branch or tag) and description.
- `export` writes `NAME.bgx.tar.gz`, or `FILE`; `-o -` writes to standard
  output.
- `import` creates the version `NEW` from a file, as a branch, or a tag with
  `--tag`.

See [Versions](versions.html#export-and-import) for the file format.

## bgexplorer-jobs

Look after [background jobs](admin.html#background-jobs).

```sh
bgexplorer-jobs [--uri URI] list [--status S] [--kind K] [--version V] [--user U] [--limit N]
bgexplorer-jobs [--uri URI] cancel ID
bgexplorer-jobs [--uri URI] abandon ID
bgexplorer-jobs [--uri URI] clear [--all] [--files-dir DIR]
```

- `list` prints the latest jobs, newest first: id, status, kind, version, user,
  when it was created and the first line of its error.
- `cancel` removes a queued job, or asks a running one to stop at its next safe
  point.
- `abandon` gives up on a running job that is stuck, so that the jobs behind it
  can run. It doesn't stop whatever runs the job, or release the version's lock;
  see `bgexplorer-versions`.
- `clear` removes finished jobs, or with `--all` also the ones waiting to start,
  and the files they left in `--files-dir` (or `FLASK_JOBS_FILES_DIR`, by default
  the same folder the server uses when it isn't configured).
