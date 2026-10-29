# Lark wiki migration

Migrates the Lark product-manual wiki into this Mintlify repo. Wraps `lark-cli`, so it stays a rerunnable tool, not a one-off import.

## Prerequisites

- Python 3.9+ (stdlib only).
- `lark-cli` on PATH, logged in as user: `lark-cli login --as user`.

## Usage

All commands run from anywhere; they operate on the repo root two levels up.

```bash
python3 scripts/lark-migrate/migrate.py plan                     # walk wiki, write .manifest.json
python3 scripts/lark-migrate/migrate.py sync                     # fetch pages, write MDX + images
python3 scripts/lark-migrate/migrate.py nav                      # regenerate docs.json navigation
python3 scripts/lark-migrate/migrate.py prune                    # delete mdx/images/state the manifest+navigation no longer reference
python3 scripts/lark-migrate/migrate.py all                      # plan + sync + nav
```

`prune` is deliberately not part of `all`: deleting files must stay an explicit step.

Global flags (accepted by every subcommand):

- `--lang <code>` — limit to one or more language dirs (`zh-Hans`, `ja`, `ko`, `zh-Hant`, `en`), repeatable.
- `--only <glob>` — match against the output file path, e.g. `--only 'zh-Hans/agent-builder/*'`. Affects `sync` writes and `plan` output; `nav` always rebuilds whole language groups.
- `--force` — re-sync pages even when unchanged.
- `--dry-run` — print actions, write nothing.

## How files land

- Language wiki dirs map to the repo's language dirs via `language_rules` in `config.json`; the language dir itself is a cover page and never becomes a file.
- Pages (and their whole subtree) whose title matches `skip_page_title_patterns` in `config.json` are never migrated. This currently filters the internal-only Technical Manual category in every language plus any `[内部文档]` / `[社内文書]` / `[Internal]` marked page. After changing these patterns, run `plan`, `nav`, then `prune` to remove the previously synced files and their images.
- A leaf page becomes `<lang>/<slug>.mdx`. A page with children becomes `<lang>/<slug>/index.mdx` and its children live beside it.
- Slugs: first matching `slug_overrides` entry wins; otherwise the ASCII text of the last ASCII-containing parenthetical, e.g. `Agent生成器（Agent Builder）` → `agent-builder`; otherwise all ASCII words of the title; if the title has no ASCII at all, the raw title is the filename. Collisions inside one directory get `-2`, `-3` suffixes.
- Page order in `docs.json` follows the wiki order per language. Each language gets a single group named after the wiki dir title minus its language suffix (e.g. `Agent Lab产品使用手册`).
- Images download to `images/<lang>/<page-path>/img-<lark-token>.<ext>` (each page gets its own directory, mirroring its MDX path without the extension) and the MDX references them as `/images/...`. The token-named path is stable: inserting an image in the middle of a page never renames the others.
- Sync never replaces a file with identical content. A page whose `obj_edit_time` changed is re-fetched, but the MDX is only rewritten when its bytes differ, and an image whose token already exists on disk is reused or renamed in place, not re-downloaded. Lark media tokens address immutable content, which is what makes token reuse safe. Only images whose tokens disappeared from the page are deleted.

## Refreshing after wiki edits

Run `plan` again, then `sync`. A page is skipped when its Lark `obj_edit_time` is unchanged, its MDX exists, and all its recorded images exist. A re-synced page keeps unchanged images in place and rewrites its MDX only when the content actually differs. `nav` rewrites a language's groups only when the result differs. `.state.json` is gitignored; delete it to force a full re-sync.

## Adding a language

Add a rule to `language_rules` in `config.json` (regex on the wiki dir title → Mintlify language dir) and add the language to `navigation.languages` in `docs.json` if it is not there yet. Then run `plan` and `sync --lang <code>`.

## Adding a slug override

Append to `slug_overrides` in `config.json` as `{"pattern": "<regex on the wiki title>", "slug": "<slug>"}`. First match wins. Then rerun `plan`; if the page was already synced, delete its old output file or pass `--force`, since a slug change moves the file.
