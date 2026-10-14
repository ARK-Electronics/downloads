#!/usr/bin/env python3
"""Publish release files to downloads.arkelectron.com.

Usage: downloads.py publish DIR PREFIX [--exclude GLOB]...   upload DIR to PREFIX/, once
       downloads.py promote SRC DEST                          copy release folder SRC/ to DEST/ under fixed names
       downloads.py index                                     rewrite every folder's index.html and the root manifest.json

DIR must hold a SHA256SUMS covering every file in it; --exclude leaves matching files
and their lines out. promote writes a VERSION file holding SRC's last path segment.

The site is an S3 bucket behind CloudFront, which serves <dir>/index.html for <dir>/.
Release folders are written once and cached for a year. latest/, the indexes and the
manifest change in place, so they are cached for a minute instead of being invalidated.
Needs the aws CLI with credentials that can write the bucket named in DOWNLOADS_BUCKET.
"""

import argparse
import os
import fnmatch
import hashlib
import json
import mimetypes
import posixpath
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote

IMMUTABLE = "public, max-age=31536000, immutable"
MUTABLE = "public, max-age=60"
SUMS = "SHA256SUMS"
TEMPLATE = Path(__file__).with_name("page.html")
DESCRIPTIONS = {
    "firmware/": "PX4 firmware for ARK CAN nodes and flight controllers",
    "jetson/": "Flash packages for ARK Jetson carrier boards",
    "pi/": "Golden images for ARK Raspberry Pi carrier boards",
}

# Builds shared by direct link for testing; kept off the index pages and manifest, and S3 deletes them after 90 days
UNLISTED = "dev/"


def bucket() -> str:
    name = os.environ.get("DOWNLOADS_BUCKET")
    if not name:
        sys.exit("set DOWNLOADS_BUCKET to the site's S3 bucket")
    return name


def aws(*args: str) -> bytes:
    return subprocess.run(["aws", *args], check=True, stdout=subprocess.PIPE).stdout


def list_objects(prefix: str = "") -> list[dict]:
    out = aws("s3api", "list-objects-v2", "--bucket", bucket(), "--prefix", prefix, "--output", "json")
    return json.loads(out or b"{}").get("Contents") or []


def get(key: str) -> bytes:
    return aws("s3", "cp", f"s3://{bucket()}/{key}", "-")


def content_type(name: str) -> str:
    if name in (SUMS, "VERSION"):
        return "text/plain; charset=utf-8"
    if name.endswith(".html"):
        return "text/html; charset=utf-8"
    mime, encoding = mimetypes.guess_type(name)
    # A .tar.gz is a gzip file to the browser, not a tar
    if encoding:
        return {"gzip": "application/gzip", "xz": "application/x-xz", "bzip2": "application/x-bzip2"}.get(encoding, "application/octet-stream")
    return mime or "application/octet-stream"


def put(key: str, data: bytes, cache_control: str) -> None:
    with tempfile.NamedTemporaryFile() as body:
        body.write(data)
        body.flush()
        aws("s3api", "put-object", "--bucket", bucket(), "--key", key, "--body", body.name,
            "--content-type", content_type(posixpath.basename(key)), "--cache-control", cache_control)
    print(f"put {key}")


def copy(source: str, key: str, cache_control: str, create_only: bool = False) -> None:
    """Copy a local path or s3:// URL to key; aws s3 cp streams it and goes multipart for large files."""
    args = ["s3", "cp", source, f"s3://{bucket()}/{key}", "--only-show-errors",
            "--content-type", content_type(posixpath.basename(key)), "--cache-control", cache_control]
    if source.startswith("s3://"):
        args += ["--metadata-directive", "REPLACE"]
    if create_only:
        args.append("--no-overwrite")
    aws(*args)
    print(f"put {key}")


def folder(prefix: str) -> str:
    return prefix.strip("/") + "/"


def parse_sums(text: bytes) -> dict[str, str]:
    sums = {}
    for line in text.decode().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
    return sums


def format_sums(sums: dict[str, str]) -> bytes:
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())).encode()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(src: Path, prefix: str, exclude: list[str]) -> None:
    prefix = folder(prefix)
    if list_objects(prefix):
        sys.exit(f"{prefix} already exists; a published release is never replaced")

    def kept(name: str) -> bool:
        return not any(fnmatch.fnmatch(name, g) for g in exclude)

    sums = {n: d for n, d in parse_sums((src / SUMS).read_bytes()).items() if kept(n)}
    files = {p.name: p for p in src.iterdir() if p.is_file() and p.name != SUMS and kept(p.name)}
    if set(files) != set(sums):
        sys.exit(f"{SUMS} lists {sorted(sums)}, files are {sorted(files)}")
    for name, path in files.items():
        if sha256(path) != sums[name]:
            sys.exit(f"{name} does not match {SUMS}")

    for name, path in sorted(files.items()):
        copy(str(path), prefix + name, IMMUTABLE, create_only=True)
    # Last, so a folder with SHA256SUMS is complete
    put(prefix + SUMS, format_sums(sums), IMMUTABLE)


def fixed_name(name: str) -> str:
    # PX4 names the application after its build hash, so latest/ renames it to stay linkable
    if name.endswith(".uavcan.bin"):
        return "application.uavcan.bin"
    if name.endswith(".px4"):
        return "application.px4"
    if name.endswith("bootloader.bin"):
        return "bootloader.bin"
    return name


def promote(src: str, dest: str) -> None:
    src, dest = folder(src), folder(dest)
    if not list_objects(src + SUMS):
        sys.exit(f"{src} is not published")

    # Release folders were verified on publish and never change, so copy server side
    sums = parse_sums(get(src + SUMS))
    renamed = {fixed_name(n): (n, d) for n, d in sums.items()}
    if len(renamed) != len(sums):
        sys.exit(f"fixed names collide: {sorted(sums)}")
    keep = set(renamed) | {SUMS, "VERSION"}
    stale = [o["Key"] for o in list_objects(dest) if o["Key"][len(dest):] not in keep]

    for new, (old, _) in sorted(renamed.items()):
        copy(f"s3://{bucket()}/{src}{old}", dest + new, MUTABLE)
    put(dest + SUMS, format_sums({n: d for n, (_, d) in renamed.items()}), MUTABLE)
    put(dest + "VERSION", f"{posixpath.basename(src.rstrip('/'))}\n".encode(), MUTABLE)
    for key in stale:
        aws("s3api", "delete-object", "--bucket", bucket(), "--key", key)
        print(f"deleted {key}")


def generated(key: str) -> bool:
    return key == "manifest.json" or posixpath.basename(key) == "index.html"


def natural(name: str) -> list:
    return [int(s) if s.isdigit() else s for s in re.split(r"([0-9]+)", name)]


def sort_dirs(names: set[str]) -> list[str]:
    # Release folders newest first, under latest/; anything else alphabetical
    rest = sorted(names - {"latest"}, key=natural)
    if rest and all(re.match(r"v?[0-9]", n) for n in rest):
        rest.reverse()
    return (["latest"] if "latest" in names else []) + rest


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def day(stamp: str) -> str:
    return datetime.fromisoformat(stamp).strftime("%Y-%m-%d") if stamp else ""


def page(template: Template, prefix: str, dirs: list[tuple[str, str, str]], files: list[dict],
         sums: dict[str, str]) -> bytes:
    """One folder's index; dirs are (name, newest change inside, badge)."""
    parts = prefix.rstrip("/").split("/") if prefix else []
    crumbs = ['<a href="/">downloads</a>'] + [
        f'<a href="/{quote("".join(p + "/" for p in parts[:i + 1]))}">{escape(parts[i])}</a>' for i in range(len(parts) - 1)]
    if parts:
        crumbs.append(f"<span>{escape(parts[-1])}</span>")

    rows = []
    if prefix:
        rows.append('<li class="row dir"><a class="name" href="../"><svg class="icon"><use href="#i-up"/></svg>'
                    '<span class="label">Parent folder</span></a><span></span><span></span></li>')
    for name, modified, badge in dirs:
        desc = DESCRIPTIONS.get(prefix + name + "/")
        rows.append(
            f'<li class="row dir"><a class="name" href="{quote(name)}/"><svg class="icon"><use href="#i-dir"/></svg>'
            f'<span class="label mono">{escape(name)}{f"""<span class="desc">{escape(desc)}</span>""" if desc else ""}</span>'
            f'{f"""<span class="pill">{escape(badge)}</span>""" if badge else ""}</a>'
            f'<span class="meta size"></span><span class="meta date">{day(modified)}</span></li>')
    for f in files:
        name = f["Key"][len(prefix):]
        sha = sums.get(name)
        checksum = (f'<div class="sha"><span class="mono">sha256 {sha[:12]}…{sha[-12:]}</span>'
                    f'<button type="button" data-sha="{sha}">Copy</button></div>') if sha else ""
        rows.append(
            f'<li class="row file"><a class="name" href="{quote(name)}"><svg class="icon"><use href="#i-file"/></svg>'
            f'<span class="label mono">{escape(name)}</span></a><span class="meta size">{human_size(f["Size"])}</span>'
            f'<span class="meta date">{day(f.get("LastModified", ""))}</span>{checksum}</li>')
    if not dirs and not files:
        rows.append('<li class="empty">Nothing published here yet.</li>')

    heading = parts[-1] if parts else "Downloads"
    return template.substitute(title=escape(heading), heading=escape(heading),
                               crumbs="<span>/</span>".join(crumbs), rows="\n".join(rows)).encode()


def build_site(objects: list[dict], sums: dict[str, dict[str, str]], badges: dict[str, str]) -> dict[str, bytes]:
    """Index pages and manifest for the given objects.

    sums maps a folder prefix to its SHA256SUMS entries, badges a folder prefix to the label shown on its row.
    """
    sizes = {o["Key"]: o["Size"] for o in objects}
    manifest = {"files": {prefix + name: {"sha256": digest, "size": sizes[prefix + name]}
                          for prefix, entries in sums.items() for name, digest in entries.items()
                          if prefix + name in sizes}}
    manifest["files"] = dict(sorted(manifest["files"].items()))
    site = {"manifest.json": (json.dumps(manifest, indent=1) + "\n").encode()}

    dirs: dict[str, set[str]] = {"": set()}
    newest: dict[str, str] = {}
    files: dict[str, list[dict]] = {"": [{"Key": "manifest.json", "Size": len(site["manifest.json"])}]}
    for o in objects:
        parts = o["Key"].split("/")
        for i in range(len(parts) - 1):
            parent = "".join(p + "/" for p in parts[:i])
            child = parent + parts[i] + "/"
            dirs.setdefault(parent, set()).add(parts[i])
            dirs.setdefault(child, set())
            newest[child] = max(newest.get(child, ""), o.get("LastModified", ""))
        files.setdefault(posixpath.dirname(o["Key"]) + "/" if "/" in o["Key"] else "", []).append(o)

    template = Template(TEMPLATE.read_text())
    for prefix in dirs:
        # Checksums and VERSION after the files they describe
        listed = sorted(files.get(prefix, []), key=lambda f: (posixpath.basename(f["Key"]) in (SUMS, "VERSION"), natural(f["Key"])))
        subdirs = [(d, newest.get(prefix + d + "/", ""), badges.get(prefix + d + "/", "")) for d in sort_dirs(dirs[prefix])]
        site[prefix + "index.html"] = page(template, prefix, subdirs, listed, sums.get(prefix, {}))
    return site


def index() -> None:
    def listed() -> list[dict]:
        return [o for o in list_objects() if not o["Key"].startswith(UNLISTED)]

    def content(objects: list[dict]) -> list[tuple[str, str]]:
        return sorted((o["Key"], o["ETag"]) for o in objects if not generated(o["Key"]))

    # A publish running at the same time can land after the listing; rebuild until the listing holds
    for _ in range(5):
        objects = listed()
        snapshot = content(objects)
        sources = [o for o in objects if not generated(o["Key"])]
        sums = {key[:-len(SUMS)]: parse_sums(get(key)) for key, _ in snapshot if posixpath.basename(key) == SUMS}
        latest = {key[:-len("VERSION")]: get(key).decode().strip() for key, _ in snapshot if key.endswith("/latest/VERSION")}
        etags = {o["Key"]: o["ETag"].strip('"') for o in objects}
        for key, data in sorted(build_site(sources, sums, latest).items()):
            if etags.get(key) != hashlib.md5(data).hexdigest():
                put(key, data, MUTABLE)
        if content(listed()) == snapshot:
            return
    sys.exit("the bucket kept changing while indexing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("publish")
    p.add_argument("dir", type=Path)
    p.add_argument("prefix")
    p.add_argument("--exclude", action="append", default=[])
    p = sub.add_parser("promote")
    p.add_argument("src")
    p.add_argument("dest")
    sub.add_parser("index")
    args = parser.parse_args()
    if args.command == "publish":
        publish(args.dir, args.prefix, args.exclude)
    elif args.command == "promote":
        promote(args.src, args.dest)
    else:
        index()


if __name__ == "__main__":
    main()
