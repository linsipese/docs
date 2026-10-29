#!/usr/bin/env python3
"""Migrate the Lark product-manual wiki into this Mintlify repo.

Wraps `lark-cli` (must be logged in: `lark-cli login --as user`).
Subcommands: plan | sync | nav | all. See README.md next to this file.
"""
import argparse
import fnmatch
import glob
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
CONFIG = SCRIPT_DIR / "config.json"
MANIFEST = SCRIPT_DIR / ".manifest.json"
STATE = SCRIPT_DIR / ".state.json"
DOCS_JSON = REPO_ROOT / "docs.json"

RATE_LIMIT_MARKERS = ("rate_limit", "99991400")

PAREN_RE = re.compile(r"[（(]([^（()）]*)[)）]")
WORD_RE = re.compile(r"[A-Za-z0-9]+")
HEADING_RE = re.compile(r"^(#{1,6})(\s)")
TITLE_TAG_RE = re.compile(r"^<title>.*</title>\s*\n?", re.DOTALL)
SUB_PAGE_LIST_RE = re.compile(r"<sub-page-list[^>]*>.*?</sub-page-list>\s*\n?", re.DOTALL)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(https://feishu\.cn/file/([A-Za-z0-9]+)\)")


class LarkError(Exception):
    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


# --- lark-cli boundary -------------------------------------------------


def run_lark(args):
    """Run one lark-cli call and return its parsed stdout JSON.

    stdout may carry a human line such as "Found N node(s)" before the JSON,
    so parsing starts at the first '{'. Rate limits back off 60s * 2^n.
    """
    for attempt in range(4):
        proc = subprocess.run(
            ["lark-cli", *args, "--as", "user", "--format", "json"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        parsed = None
        start = proc.stdout.find("{")
        if start != -1:
            try:
                parsed = json.loads(proc.stdout[start:])
            except json.JSONDecodeError:
                parsed = None
        if proc.returncode == 0 and parsed is not None and parsed.get("ok"):
            return parsed
        blob = proc.stdout + proc.stderr
        if any(marker in blob for marker in RATE_LIMIT_MARKERS):
            if attempt == 3:
                raise LarkError(f"rate limit persisted after retries: {args}", True)
            time.sleep(60 * (2 ** attempt))
            continue
        raise LarkError(f"lark-cli failed ({proc.returncode}): {' '.join(args)}\n{blob[:400]}")
    raise LarkError(f"unreachable: exhausted retries: {args}")


def load_config():
    cfg = json.loads(CONFIG.read_text())
    for key in ("wiki_root", "language_rules", "skip_title_patterns", "slug_overrides"):
        if key not in cfg:
            sys.exit(f"config.json missing key: {key}")
    return cfg


def load_json(path, hint):
    if not path.exists():
        sys.exit(f"{path.name} not found — {hint}")
    return json.loads(path.read_text())


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


# --- pure text transforms ----------------------------------------------


def demote_headings(md):
    """Demote every ATX heading one level (capped at h6), leaving fenced code alone."""
    out = []
    in_fence = False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        m = HEADING_RE.match(line)
        if not in_fence and m:
            out.append("#" * min(len(m.group(1)) + 1, 6) + line[len(m.group(1)):])
        else:
            out.append(line)
    return "\n".join(out)


def strip_leading_title_heading(md, title):
    m = re.match(r"^\s*#{1,6}\s+(.*?)\s*(\n|$)", md)
    if m and m.group(1) == title.strip():
        return md[m.end():].lstrip("\n")
    return md


def transform(md, title):
    md = TITLE_TAG_RE.sub("", md, count=1)
    md = SUB_PAGE_LIST_RE.sub("", md)
    md = demote_headings(md)
    md = strip_leading_title_heading(md, title)
    return md.strip() + "\n"


def frontmatter(title):
    return "---\ntitle: " + json.dumps(title, ensure_ascii=False) + "\n---\n\n"


def ordered_unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --- slugs --------------------------------------------------------------


def kebab_ascii(text):
    return "-".join(w.lower() for w in WORD_RE.findall(text))


def make_slug(title, overrides):
    for rule in overrides:
        if re.search(rule["pattern"], title):
            return rule["slug"]
    paren_slugs = [kebab_ascii(inner) for inner in PAREN_RE.findall(title)]
    paren_slugs = [s for s in paren_slugs if s]
    if paren_slugs:
        return paren_slugs[-1]
    if kebab_ascii(title):
        return kebab_ascii(title)
    return title.strip().replace("/", "-").replace("\\", "-") or "page"


def dedupe(slug, used):
    used[slug] = used.get(slug, 0) + 1
    return slug if used[slug] == 1 else f"{slug}-{used[slug]}"


# --- wiki tree (plan) ---------------------------------------------------


def list_children(space_id, parent):
    out = run_lark(["wiki", "+node-list", "--space-id", space_id,
                    "--parent-node-token", parent, "--page-all"])
    return out["data"]["nodes"]


def resolve_root(cfg):
    root = cfg["wiki_root"]
    if cfg.get("space_id") and not root.startswith("http"):
        return cfg["space_id"], root
    out = run_lark(["wiki", "+node-get", "--node-token", root])
    return out["data"]["space_id"], out["data"]["node_token"]


def build_page(space_id, node, parent_dir, cfg):
    slug = dedupe(make_slug(node["title"], cfg["slug_overrides"]), parent_dir["used"])
    page = {
        "node_token": node["node_token"],
        "obj_token": node["obj_token"],
        "title": node["title"],
        "slug": slug,
    }
    if node.get("has_child"):
        dir_rel = f"{parent_dir['path']}/{slug}"
        page["file"] = f"{dir_rel}/index.mdx"
        page["children"] = [
            build_page(space_id, child, {"path": dir_rel, "used": {}}, cfg)
            for child in list_children(space_id, node["node_token"])
        ]
    else:
        page["file"] = f"{parent_dir['path']}/{slug}.mdx"
    return page


def iter_pages(pages):
    for page in pages:
        yield page
        yield from iter_pages(page.get("children", []))


def cmd_plan(args):
    cfg = load_config()
    space_id, root = resolve_root(cfg)
    languages, skipped = [], []
    for node in list_children(space_id, root):
        title = node["title"]
        if any(re.search(p, title) for p in cfg["skip_title_patterns"]):
            skipped.append(title)
            continue
        lang = next((r["dir"] for r in cfg["language_rules"] if re.search(r["pattern"], title)), None)
        if lang is None:
            skipped.append(title)
            continue
        if args.lang and lang not in args.lang:
            continue
        languages.append({
            "lang": lang,
            "node_token": node["node_token"],
            "dir_title": title,
            "group_name": title.rsplit(" - ", 1)[0] if " - " in title else title,
            "pages": [build_page(space_id, child, {"path": lang, "used": {}}, cfg)
                      for child in list_children(space_id, node["node_token"])],
        })
    manifest = {"space_id": space_id, "root_node_token": root, "languages": languages}
    if args.dry_run:
        print("would write " + MANIFEST.name)
    else:
        save_json(MANIFEST, manifest)
    for lang in languages:
        pages = list(iter_pages(lang["pages"]))
        categories = [p["slug"] for p in lang["pages"] if p.get("children")]
        print(f"{lang['lang']}: {len(pages)} pages, categories: {', '.join(categories)}")
        if args.only:
            for p in pages:
                if fnmatch.fnmatch(p["file"], args.only):
                    print(f"  match {p['file']}  {p['title']!r}")
    for title in skipped:
        print(f"skipped dir: {title!r}")
    no_ascii = [p["title"] for lang in languages for p in iter_pages(lang["pages"])
                if not WORD_RE.search(p["title"])]
    if no_ascii:
        print("titles with no ASCII (raw-title filename fallback):")
        for title in no_ascii:
            print(f"  {title!r}")


# --- sync ---------------------------------------------------------------


def sync_page(page, state, args):
    prev = state.get(page["node_token"])
    info = run_lark(["wiki", "+node-get", "--node-token", page["node_token"]])["data"]
    edit_time = info.get("obj_edit_time", "")
    if (prev and not args.force and prev.get("editTime") == edit_time
            and (REPO_ROOT / prev["file"]).exists()
            and all((REPO_ROOT / rec["path"]).exists() for rec in prev.get("images", []))):
        print(f"skip    {page['file']} (unchanged)")
        return
    if args.dry_run:
        print(f"would sync {page['file']}  {page['title']!r}")
        return
    doc = run_lark(["docs", "+fetch", "--doc", page["obj_token"], "--doc-format", "markdown"])
    body = transform(doc["data"]["document"]["content"], page["title"])
    tokens = ordered_unique(IMAGE_RE.findall(body))
    # Re-sync re-numbers images from 1, so clear the previous run's files first.
    for rec in (prev or {}).get("images", []):
        Path(REPO_ROOT / rec["path"]).unlink(missing_ok=True)
    img_dir = "images/" + page["file"][: -len(".mdx")]
    images, mapping = [], {}
    for i, token in enumerate(tokens, 1):
        (REPO_ROOT / img_dir).mkdir(parents=True, exist_ok=True)
        base = f"{img_dir}/img-{i}"
        # --output takes NO extension: the CLI appends the real one from the
        # Content-Type, so the actual filename has to be globbed back afterwards.
        run_lark(["docs", "+media-download", "--token", token, "--output", base])
        found = glob.glob(str(REPO_ROOT / (base + ".*")))
        if not found:
            raise LarkError(f"no file produced for image token {token}")
        rel = str(Path(found[0]).relative_to(REPO_ROOT))
        mapping[token] = "/" + rel
        images.append({"token": token, "path": rel})
    body = IMAGE_RE.sub(lambda m: f"![]({mapping[m.group(1)]})", body)
    out_file = REPO_ROOT / page["file"]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(frontmatter(page["title"]) + body)
    state[page["node_token"]] = {
        "title": page["title"], "slug": page["slug"], "file": page["file"],
        "editTime": edit_time, "images": images,
    }
    print(f"synced  {page['file']}  ({len(images)} images)")


def cmd_sync(args):
    manifest = load_json(MANIFEST, "run `plan` first")
    state = load_json(STATE, "") if STATE.exists() else {}
    pages = [p for lang in manifest["languages"]
             if not args.lang or lang["lang"] in args.lang
             for p in iter_pages(lang["pages"])]
    if args.only:
        pages = [p for p in pages if fnmatch.fnmatch(p["file"], args.only)]
    errors = 0
    for page in pages:
        try:
            sync_page(page, state, args)
            if not args.dry_run:
                save_json(STATE, state)
        except LarkError as exc:
            errors += 1
            print(f"ERROR   {page['file']}: {exc}", file=sys.stderr)
    print(f"{len(pages)} page(s) considered, {errors} error(s)")
    if errors:
        sys.exit(1)


# --- nav ----------------------------------------------------------------


def nav_entry(page):
    path = page["file"][: -len(".mdx")]
    if page.get("children"):
        return {"group": page["title"],
                "pages": [path] + [nav_entry(child) for child in page["children"]]}
    return path


def cmd_nav(args):
    manifest = load_json(MANIFEST, "run `plan` first")
    doc = json.loads(DOCS_JSON.read_text())
    for lang in manifest["languages"]:
        if args.lang and lang["lang"] not in args.lang:
            continue
        group = {"group": lang["group_name"],
                 "pages": [nav_entry(p) for p in lang["pages"]]}
        entry = next((e for e in doc["navigation"]["languages"]
                      if e.get("language") == lang["lang"]), None)
        if entry is None:
            doc["navigation"]["languages"].append({"language": lang["lang"], "groups": [group]})
        else:
            entry["groups"] = [group]
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(f"would rewrite docs.json")
    elif DOCS_JSON.read_text() != text:
        DOCS_JSON.write_text(text)
        print("updated docs.json")
    else:
        print("docs.json already up to date")


# --- prune ---------------------------------------------------------------


def nav_refs(entry):
    refs = []
    for group in entry.get("groups", []):
        for page in group.get("pages", []):
            if isinstance(page, str):
                refs.append(page)
            else:
                stack = [page]
                while stack:
                    node = stack.pop()
                    for sub in node.get("pages", []):
                        if isinstance(sub, str):
                            refs.append(sub)
                        else:
                            stack.append(sub)
    return refs


def cmd_prune(args):
    """Delete mdx files under managed language dirs that the navigation no
    longer references (e.g. replaced template stubs). Explicit on purpose:
    never part of `all`."""
    manifest = load_json(MANIFEST, "run `plan` first")
    doc = json.loads(DOCS_JSON.read_text())
    managed = {lang["lang"] for lang in manifest["languages"]}
    referenced = set()
    for entry in doc["navigation"]["languages"]:
        if entry.get("language") in managed:
            referenced.update(nav_refs(entry))
    removed = 0
    for lang_dir in sorted(managed):
        for mdx in sorted((REPO_ROOT / lang_dir).rglob("*.mdx")):
            ref = str(mdx.relative_to(REPO_ROOT))[: -len(".mdx")]
            if ref in referenced:
                continue
            print(f"{'would delete' if args.dry_run else 'delete'}  {ref}.mdx")
            if not args.dry_run:
                mdx.unlink()
            removed += 1
    print(f"{removed} orphan file(s){' (dry-run)' if args.dry_run else ''}")


# --- CLI ----------------------------------------------------------------


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lang", action="append",
                        help="limit to language dir(s), repeatable")
    common.add_argument("--only", help="glob matched against the output file path")
    common.add_argument("--force", action="store_true", help="re-sync even if unchanged")
    common.add_argument("--dry-run", action="store_true", help="print actions, write nothing")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    funcs = {"plan": cmd_plan, "sync": cmd_sync, "nav": cmd_nav, "prune": cmd_prune}
    for name, func in funcs.items():
        p = sub.add_parser(name, parents=[common])
        p.set_defaults(func=func)
    p = sub.add_parser("all", parents=[common])
    p.set_defaults(func=lambda a: (cmd_plan(a), cmd_sync(a), cmd_nav(a)))
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
