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
| `src/bgexplorer/models/` | the MongoEngine documents and the calculations: components, emission specs, hit efficiencies, SourceTerms and results, version control, uncertainties (`asymmetric.py`), histograms and units |
| `src/bgexplorer/application/` | the Flask app: pages (`blueprints.py`), versions, admin, login, JSON API (`api.py`), templates and static files |
| `src/bgexplorer/cli.py` | the `bgexplorer-users` and `bgexplorer-versions` commands |
| `examples/` | example models (`qis`) and scripts; not installed with the package |
| `tests/` | unittest-style tests, run with pytest |
| `scripts/profile_calc.py` | a manual benchmark of the calculations on the example model |

Most documents inherit from `VersionedDocument` (`models/verdoc.py`), which
implements in-database version control. Physical quantities are pint
quantities with units, and uncertainties are `AsymmetricUncertainty` values.

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
