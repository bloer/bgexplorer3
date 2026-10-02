# Background Explorer

A web application for building and tracking the radioactive background model
of a radiation detector. The full documentation is at
<https://bloer.github.io/bgexplorer3/> (source in [docs/](docs/)).

## Running a server

The simplest way is with docker compose, which runs the web server and a
MongoDB database whose data are kept in the `mongo-data` volume:

```sh
docker compose up -d
docker compose logs web | grep token  # the one-time setup token
```

Then open http://localhost:8000 and follow the setup link to create the first
site admin (see below). Set `BGEXPLORER_PORT` to use another port.

The server is configured with environment variables. Any Flask setting can be
given as `FLASK_<NAME>`:

- `FLASK_MONGODB_URI`: the database, e.g. `mongodb://mongo:27017/bgexplorer`.
- `FLASK_SECRET_KEY`: optional, see below.
- `FLASK_SESSION_COOKIE_SECURE=true`: when users reach the server over https,
  e.g. through a reverse proxy, which should also handle TLS.
- `GUNICORN_CMD_ARGS`: gunicorn's options, by default
  `--workers 2 --threads 4 --timeout 120 --access-logfile -`.

Without docker, install with `pip install '.[server]'` and run e.g.
`gunicorn --bind 0.0.0.0:8000 'bgexplorer:create_app()'`. The user commands
also work inside the container, e.g.
`docker compose exec web bgexplorer-users create NAME --role site_admin`.

For backups, export versions (see "Versions"), or back up the whole database
with `mongodump`, which also keeps the users and site settings.

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

A `SECRET_KEY` protects the login sessions. If one isn't configured, e.g. with
the `FLASK_SECRET_KEY` environment variable, the server generates one the first
time it starts and keeps it in the database's `server_secrets` collection, so
it stays the same across restarts and is shared by every worker. Anyone who
can read the database can then forge logins, but they could already add users.
To end everyone's sessions, delete that document (or change the configured
key) and restart.

Until there is a site admin, every page links to `/setup`, which creates the
first one. It asks for a one-time setup token, which the server writes to its
log when it starts, so only someone who can see the log can claim a new
server. They can then add other users through the web interface.

Users can also be managed on the command line; `create` asks for a
password. The commands only
connect to the database, given by `--uri` or the `FLASK_MONGODB_URI`
environment variable (`python -m bgexplorer.cli` also works when the package
isn't installed):

```sh
bgexplorer-users --uri mongodb://HOST/DATABASE create NAME --role site_admin
bgexplorer-users set-password NAME  # if a password is forgotten
```

Set `LOGIN_DISABLED = True` in the configuration to give everyone every role,
e.g. for a server only you can reach.

## Versions

A model is one version: a branch, which can be edited, or a tag, a read-only
snapshot. Each document (component, emission spec, hit efficiency or
activated material) belongs to a list of versions. Versions share a copy of a
document until one of them changes it. All copies of an item have the same
`original_id`, which references use.

- **Compare**: the overview of a version compares it with any other. Items
  are listed as changed, only in one version, or the same. Each item has a
  diff page that shows the differences between its copies, linked from the
  "other versions" table on its page.
- **Import**: the diff page can import one item from the other version,
  replacing the copy in this version. Only that document is imported, so
  everything it refers to must already be in this version.
- **Merge**: "Merge into" on a branch's overview merges another version into
  it. Items only in the other version are added, and nothing is removed.
  For items that are different in both, and for the settings, one rule
  decides which copy is kept: `newest` (changed last; for the settings,
  whichever version changed last), `source` (the version merged in) or
  `target` (the branch merged into). The version's name, description and
  editability are always kept. The page previews the result; confirming
  merges it, unless either version changed since the preview. The JSON API
  has the same, at `/api/v1/versions/<version>/merge`.

MongoDB transactions need a replica set, so merges don't use them. Instead,
both versions are locked while merging, and nobody else can change them.
The target is backed up as a tag first, and restored from it if the merge
fails; the backup is deleted afterwards. If even restoring fails, the backup
tag is kept and named in the error. An admin can clear a lock that an
interrupted merge left behind, from the version's overview.

A whole version can be exported to a file, e.g. to move it to another
server or keep a backup, and imported under a new name: "Export" on its
overview, "Import a version from a file" on the new-version page, the API
(`GET /api/v1/versions/<version>/export`, and a multipart
`POST /api/v1/versions/import` with `file`, `version_tag`, `type` and
`description`), or the command line:

```sh
bgexplorer-versions --uri mongodb://HOST/DATABASE export NAME -o NAME.bgx.tar.gz
bgexplorer-versions --uri mongodb://HOST/DATABASE import NAME.bgx.tar.gz --name NEW [--tag]
```

The file is a gzipped tar of `manifest.json`, `settings.json` and one JSON
lines file per collection, in MongoDB Extended JSON. Imported documents get
new copies, but keep their `original_id`, so references still work and the
new version can be compared with and merged into versions it came from.
The whole file is checked before anything is created. SourceTerms aren't
exported; they are rebuilt on import.

Creating, deleting, importing into and merging versions is recorded in a
log, shown as the History on each version's overview. There are no commits:
tags are the only snapshots.

## Uncertainties and correlations

Rates, hit efficiencies and results are `AsymmetricUncertainty` values
(`bgexplorer/models/asymmetric.py`): a mode with separate lower and upper
standard deviations. Upper limits, common for assays, are represented as
`0 +s1 -0`. They combine so that a sum of limits is still a limit, and adding
a limit to a measurement doesn't raise the measurement's lower bound. The
sigmas propagate as if gaussian, so this is a practical convention rather
than a strict statistical model.

### What is correlated

Independent random variables ("leaves") are values that were entered or
measured:

- the rates of emission sources entered in a spec, e.g. an assay's U238;
- each scalar and spectrum of each hit efficiency;
- each isotope's activation rate in an activated material;
- the radon level of each period of a radon exposure.

Everything else is calculated from these by sums and products, and keeps
track of the leaves it depends on. Uncertainties from a shared leaf add
linearly rather than in quadrature, e.g.:

- one assay used by several components, or placed several times in an
  assembly;
- one hit efficiency used by several sources or components;
- activations of the same material, which share its activation rates, even
  with different exposure histories;
- sources generated by the version settings and the source they come from,
  e.g. Ra226 in equilibrium with U238.

Only sums and products are tracked. Division by an uncertain value and other
nonlinear operations treat their inputs as independent, with a warning, as do
a few array operations, such as slicing a spectrum or integrating it.

### Storing correlated values

Each leaf has an id, made when it is created (a bson ObjectId), which is
stored with its value. A leaf loaded again, e.g. the same hit efficiency in
two calculations, is the same variable. AsymmetricUncertainties can't be
changed once made, so an id always stands for one value. Editing a value
makes a new leaf, and the model recalculates and clears whatever depended on
the old one.

Calculated values are stored with their expression: the terms they're made
of, by the ids of their leaves, with the leaves' values. A loaded value is
then correlated with everything else sharing its leaves:

- computed source rates (activations, radon, generated sources) keep theirs
  inline;
- stored results (`CalculatedResults`) keep theirs in separate fields, so
  that stored results can be combined as if they were calculated together.

Expressions are much bigger than the values: a spectrum's expression includes
each hit efficiency spectrum it was made from. Results that are only shown,
like the spectra plots, are loaded without them. Such values can be shown and
scaled, e.g. to convert units, but combining them with other uncertain values
raises `CorrelationsNotLoaded`, rather than wrongly treating them as
independent.

Stored leaves are repeated in each expression that uses them, so the largest
stored results grow with the number of hit efficiency spectra they include,
towards MongoDB's 16 MB document limit for big models. `LeafStore` is the
interface for keeping leaves in one place instead; so far there is only an
in-memory implementation.

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
