# Background Explorer

## Users and login

The server has local user accounts, each with one role. Roles include the
ones before them:

- `viewer`: read everything. Only needed when anonymous viewing is off.
- `editor`: change documents, and create, edit and delete branches.
- `admin`: also create tags, which are read-only snapshots.
- `site_admin`: also change the site settings, run maintenance and manage
  users, on the `/admin` pages.

Anyone can view the models without logging in, unless "allow anon view" is
turned off in the site settings.

The server must have a `SECRET_KEY`, which protects the login sessions, e.g.
from the `FLASK_SECRET_KEY` environment variable. Keep it secret and the same
across restarts. Only debug and test servers make a temporary one.

Create the first site admin on the command line; it asks for a password.
They can then add other users through the web interface. The commands only
connect to the database, given by `--uri` or the `FLASK_MONGODB_URI`
environment variable (`python -m bgexplorer.cli` also works when the package
isn't installed):

```sh
bgexplorer-users --uri mongodb://HOST/DATABASE create NAME --role site_admin
bgexplorer-users set-password NAME  # if a password is forgotten
```

Set `LOGIN_DISABLED = True` in the configuration to give everyone every role,
e.g. for a server only you can reach.

## Running tests

Install with the development extras, then run pytest:

```sh
pip install -e '.[dev]'
python -m pytest
```

Most tests need a running MongoDB server, since mongomock doesn't support
the aggregations used for version control. Tests that need it are skipped if
the server isn't available. For example, to start a temporary server with
docker:

```sh
docker run --rm -d -p 127.0.0.1:27017:27017 --name bgx-test-mongo mongo
```

Environment variables:

- `BGEXPLORER_TEST_MONGODB_URI`: server and database to use, default
  `mongodb://127.0.0.1:27017/bgexplorer3_unittest`. **All collections in
  this database are dropped by the tests.**
- `BGEXPLORER_TEST_REQUIRE_DB`: if set, fail instead of skipping when the
  server isn't available.
- `BGEXPLORER_TEST_EXAMPLES`: if set, also run the web app tests against the
  full example models. These take a few minutes.
