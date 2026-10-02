---
title: Versions
nav_order: 6
---

# Versions
{: .no_toc }

1. TOC
{:toc}

A model lives in a *version*. Versions work much like git branches and tags,
but inside the database:

- A **branch** can be edited by editors.
- A **tag** is a read-only snapshot. Only admins can create tags, and they can't
  be deleted from the web interface.

The version you are looking at is part of every URL, e.g.
`/explore/main/component/...`, and is shown in the navigation bar. The default
branch is `main`. Version names can't contain `/`.

## How documents are shared

Every document (component, emission spec, hit efficiency or activated
material) belongs to a list of versions. When you create a branch from another
version, the new branch shares all of its documents; nothing is copied. As
soon as you change a document in one branch, that branch gets its own copy, and
the other versions keep the old one.

All copies of the same item share an `original_id`, and references between
documents use it. That's why a component in a new branch still finds "its"
emission spec, even after the spec has been edited there.

## Creating and deleting versions

The **Versions** page lists all versions with their descriptions and when they
were last changed. From there you can:

- create a new branch or tag from an existing version;
- import a whole version from a file (see [Export and import](#export-and-import));
- delete a branch. Deleting removes the version's own copies of documents, but
  never documents that other versions still use.

Each version also has its own [settings](settings.html), edited from its
overview page.

## Comparing versions

The overview of a version can compare it with any other version. Items are
listed as changed, only in one of the versions, or the same.

Each item has a **diff** page showing the field-by-field differences between
its two copies. It's linked from the "Other versions" table at the bottom of
each item's page, which lists every version that has a copy of the item.

## Importing a single document

On the diff page you can import an item from the other version, replacing the
copy in this one. Only that document is imported, so everything it refers to
(e.g. a component's emission specs) must already be in this version. The
model's SourceTerms are updated to use it.

## Merging

**Merge into** on a branch's overview merges another version into it:

- Items only in the other version are added.
- Nothing is ever removed, including items that were deleted from the branch
  but still exist in the version being merged in.
- For items that differ between the two versions, and for the settings, one
  rule decides which copy is kept:

  | Rule | Keeps |
  |------|-------|
  | `newest` | the copy changed last. For the settings, those of whichever version changed last |
  | `source` | the copy in the version being merged in |
  | `target` | the copy in the branch being merged into |

The version's own name, description and editability are always kept.

The merge page previews exactly what will happen. Confirming carries it out,
unless either version changed after the preview was made, in which case you
are asked to review it again.

While a merge runs, both versions are locked so nobody else can change them.
The target branch is backed up as a tag first and restored from it if anything
goes wrong; the backup is deleted afterwards. If even restoring fails, the
backup tag is kept and named in the error message. If an interrupted merge
leaves a lock behind, an admin can clear it from the version's overview.

The JSON API can plan and run merges too, see [API](api.html#versions).

## Export and import

A whole version can be saved to a file, e.g. to move it to another server or
keep a backup, and imported again under a new name:

- **Export** on a version's overview, or **Import a version from a file** on the
  new-version page;
- the [API](api.html#versions);
- the [command line](cli.html#bgexplorer-versions):

  ```sh
  bgexplorer-versions --uri mongodb://HOST/DATABASE export NAME -o NAME.bgx.tar.gz
  bgexplorer-versions --uri mongodb://HOST/DATABASE import NAME.bgx.tar.gz --name NEW [--tag]
  ```

The file is a gzipped tar of `manifest.json`, `settings.json` and one JSON lines
file per collection, in MongoDB Extended JSON. Imported documents keep their
`original_id`, so the imported version can be compared with, and merged into,
the versions it came from. The whole file is checked before anything is
created. SourceTerms aren't exported; they are rebuilt on import.

## History

Creating, deleting, importing into and merging versions is recorded, and shown
as the **History** on each version's overview. There are no commits: tags are
the only snapshots, so make a tag before a change you may want to go back to.
