---
title: Development
nav_order: 13
---

# Development
{: .no_toc }

1. TOC
{:toc}

## Layout

| Path | Contents |
|------|----------|
| `src/bgexplorer/versioning/` | version control for MongoEngine documents, independent of the rest of bgexplorer (see [below](#the-versioning-package)) |
| `src/bgexplorer/models/` | the MongoEngine documents and the calculations: components, emission specs, hit efficiencies, SourceTerms and results, version settings, uncertainties (`asymmetric.py`), histograms and units |
| `src/bgexplorer/application/` | the Flask app: pages (`blueprints.py`), versions, admin, login, JSON API (`api.py`), templates and static files |
| `src/bgexplorer/cli.py` | the `bgexplorer-users` and `bgexplorer-versions` commands |
| `examples/` | example models (`qis`) and scripts; not installed with the package |
| `tests/` | unittest-style tests, run with pytest |
| `tests/versioning/` | tests of the versioning package alone, using toy document classes |
| `scripts/profile_calc.py` | a manual benchmark of the calculations on the example model |

Most documents inherit from `VersionedDocument` (`versioning/verdoc.py`), which
implements in-database version control. Physical quantities are pint
quantities with units, and uncertainties are `AsymmetricUncertainty` values.

## The versioning package

`bgexplorer.versioning` imports nothing else from bgexplorer, flask or pint, so
it could be used on its own; `tests/test_versioning_isolation.py` enforces this,
and also that its code doesn't mention bgexplorer's own concepts. Its parts:

| Module | Contents |
|--------|----------|
| `verdoc` | `VersionedDocument`, the reference and list fields, copy-on-write editing and the registry of versioned classes |
| `record` | `VersionRecord`, one document per version: its existence, description, read-only flag, lock, and a cache token that changes whenever the version's data does |
| `locks` | lease locks that expire unless refreshed, and `hold_locks` |
| `control` | creating, deleting and merging versions |
| `diff`, `archive`, `history`, `maintenance` | comparing versions, files, the event log and repairs |

The application plugs in with:

- MongoEngine's own signals: saving or deleting any `VersionedDocument` touches
  its version's record. Documents that the package writes with queryset
  operations (merge, import) send nothing, so there are two more signals in
  `versioning.hooks`:
  `version_merged(target, source=, plan=)` and
  `version_imported(tag, manifest=, counts=)`. Receivers can return a list of
  warnings, which are shown with the result, or raise to undo the merge or
  import. Connect module-level functions; blinker holds receivers weakly.
  `models/signals.py` has the application's.
- Class attributes and methods of `VersionedDocument` subclasses:
  `VERSION_REGISTER`, `VERSION_DERIVED` (the documents are rebuilt from others,
  so aren't compared, merged or exported), `VERSION_BULKY_FIELDS`,
  `VERSION_LABEL_FIELDS`, `item_label(son)` and `find_dangling(version_tag)`.
- `archive.configure(format_name, producer, producer_version)`, which names the
  file format.

The per-version settings are an ordinary versioned document of the application,
`models/settings.py:VersionSettings`, with one copy per version.

## Running the tests

Install with the development extras, then run pytest:

```sh
pip install -e '.[dev]'
python -m pytest
```

Most tests need a running MongoDB server, since mongomock doesn't support the
aggregations used for version control. Tests that need it are skipped if the
server isn't available. To start a temporary server with docker:

```sh
docker run --rm -d -p 127.0.0.1:27017:27017 --name bgx-test-mongo mongo
```

`pytest tests/versioning` runs only the tests of the versioning package.

Environment variables:

- `BGEXPLORER_TEST_MONGODB_URI`: server and database to use, default
  `mongodb://127.0.0.1:27017/bgexplorer3_unittest`. **All collections in this
  database are dropped by the tests.**
- `BGEXPLORER_TEST_REQUIRE_DB`: if set, fail instead of skipping when the
  server isn't available.
- `BGEXPLORER_TEST_EXAMPLES`: if set, also run the web app tests against the
  full example models. These take a few minutes.
- `BGEXPLORER_BROWSER_LIBS`: a directory of extra shared libraries for the
  playwright browser tests (`tests/test_browser.py`), if chromium can't find
  them on the system.

The browser tests use `playwright`, which is part of the `dev` extra. After
installing it, run `playwright install chromium` to download the browser and
`sudo playwright install-deps chromium` to install its system libraries.

## Running a development server

```sh
flask --app bgexplorer run --debug
```

The database defaults to `mongodb://127.0.0.1:27017/bgexplorer3_testing`; set
`FLASK_MONGODB_URI` to use another.

## These docs

The documentation is in `docs/`, published with GitHub Pages using the
[just-the-docs](https://just-the-docs.com) theme. See `docs/README.md` to
preview it locally.
