# Background Explorer

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
