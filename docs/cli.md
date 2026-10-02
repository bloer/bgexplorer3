---
title: Command line
nav_order: 11
---

# Command line tools
{: .no_toc }

1. TOC
{:toc}

Two commands are installed with the package. They only connect to the
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
