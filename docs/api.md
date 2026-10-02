---
title: JSON API
nav_order: 12
---

# JSON API
{: .no_toc }

1. TOC
{:toc}

{: .warning }
The API is at version `v1` and still incomplete: versions can be managed
fully, but documents can only be read, and imported as files through the web
pages. Expect changes while Background Explorer is in alpha.

## Authentication

The API uses the same login sessions as the web pages: log in through
`/login` with a client that keeps cookies.

- `GET`, `HEAD` and `OPTIONS` need the `viewer` role, i.e. no login if
  anonymous viewing is allowed.
- Everything else needs `editor`, or more where noted.
- Without a login, a request that needs one gets `401`; with too low a role,
  `403`.

Request bodies must be JSON (`Content-Type: application/json`, otherwise
`415`), except file uploads. JSON bodies can't be sent cross-site without
CORS, so the API doesn't need CSRF tokens.

Errors are returned as:

```json
{"error": {"message": "...", "fields": {...}, "problems": [...]}}
```

with `fields` (validation errors per field) and `problems` (e.g. why a merge
can't be done) only when relevant. Unknown versions give `404`.

## Versions

All paths are under `/api/v1`.

| Method and path | Body or parameters | Response |
|-----------------|--------------------|----------|
| `GET /versions` | | `{"versions": [{version_tag, description, editable, type, modified, url}]}` |
| `POST /versions` | `{version_tag, description?, from?, type?: "branch" or "tag"}`. A tag needs `from` and the `admin` role | `201` with the new version's settings, and a `Location` header. `409` if the name is taken |
| `GET /versions/<v>` | | the version's settings |
| `PATCH /versions/<v>` | any of `description`, `addsources`, `hiteffdbconfig` (`rois`, `display_scalars`, `display_spectra`, `extra_columns`) | the updated settings |
| `DELETE /versions/<v>` | | `204`. Tags and the default version are refused |
| `GET /versions/<v>/merge` | `?source=<version>&rule=newest\|source\|target` | the merge plan, see below |
| `POST /versions/<v>/merge` | `{source, rule?, fingerprint?}` | the plan carried out. `409` if the merge can't be done or the fingerprint is stale |
| `GET /versions/<v>/export` | | the version file, `<v>.bgx.tar.gz` |
| `POST /versions/import` | multipart form: `file`, `version_tag`, `type`, `description`. A tag needs `admin` | `201` with the new version's settings |

A merge plan has the `source`, `target`, `rule`, a `fingerprint` of both
versions' state, any `problems`, whether there are `changes`, whether the
settings are replaced (`replace_settings`) and how (`settings`), and per
collection (`classes`) the items to `add`, `replace`, `keep`, and those only in
the target (`target_only`). To merge only what you reviewed, pass the plan's
`fingerprint` to the `POST`.

### Example

```sh
# make a branch of main
curl -b cookies.txt -H 'Content-Type: application/json' \
     -d '{"version_tag": "test", "from": "main"}' \
     http://localhost:8000/api/v1/versions

# preview, then merge it back
curl -b cookies.txt 'http://localhost:8000/api/v1/versions/main/merge?source=test'
curl -b cookies.txt -H 'Content-Type: application/json' \
     -d '{"source": "test", "fingerprint": "..."}' \
     http://localhost:8000/api/v1/versions/main/merge
```

## Documents and results

These endpoints are part of the web pages, under
`/explore/<version>/<collection>/`, where the collection is `component`,
`emission`, `hiteff` or `activation`. They need the `viewer` role.

| Path | Response |
|------|----------|
| `api/<id>` | the document's JSON, as on its Export tab |
| `<id>/spectra.json` | a hit efficiency's or component's displayed spectra, by display name |
| `<id>/dashboard.json` | a component's budget breakdown, as used by the Results tab |
| `<id>/spectrum.json` | a component's spectrum breakdown |
| `../hitefflocations` | the list of hit efficiency locations in the version |

`dashboard.json` takes `scalar` (the result to show), `unit`, `relativeto`
and `filters`. `spectrum.json` takes `spectrum`, `groupby` (`component`,
`isotope`, `material` or `category`), `relativeto` and `filters`. `filters`
is JSON:

```json
{"root": ["<placement id>", "..."],
 "material": [{"key": "copper", "exclude": false}],
 "isotope": [{"key": "K40", "exclude": true}]}
```

`root` drills into the component tree by placement ids. For `component`
filters, the keys are lists of placement ids.

Values are returned as:

```json
{"value": 1.2, "err_minus": 0.1, "err_plus": 0.2,
 "is_limit": false, "upper_limit": 1.5, "units": "1/kg/day"}
```

where `upper_limit` is the 90% upper limit. Spectra have `bins` and
`binsunit`, and the same keys with one entry per bin, except `units`.
