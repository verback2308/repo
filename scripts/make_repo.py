#!/usr/bin/env python3
"""Generate a static Cydia/Sileo APT repository from .deb files.

Usage:
    make_repo.py --config apps.toml --debs DIR --out DIR
                 [--screenshots DIR] [--changelogs DIR]

Copies the debs to OUT/debs/ and writes Packages(.gz/.bz2/.xz), Release,
depictions/<package>.json and index.html to OUT/. Stdlib only -- no dpkg/apt
tooling required.

Packages are matched to their apps.toml entry by their Package: field. One with
no entry is still indexed, it just gets a depiction with nothing but a
changelog.

--screenshots: directory holding one subdirectory per app, named by that app's
`screenshots` key.
--changelogs: directory of <package>.jsonl files, one {"tag", "body"} object
per line (GitHub release notes; body is markdown).
"""

import argparse
import bz2
import gzip
import hashlib
import io
import json
import lzma
import shutil
import tarfile
import tomllib
from pathlib import Path

ARCHITECTURES = "iphoneos-arm iphoneos-arm64"
ARCH_LABELS = {"iphoneos-arm": "rootful", "iphoneos-arm64": "rootless"}


def ar_members(data):
    """Yield (name, bytes) for each member of an ar archive."""
    if data[:8] != b"!<arch>\n":
        raise ValueError("not an ar archive")
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        body = data[offset + 60 : offset + 60 + size]
        yield name, body
        offset += 60 + size + (size % 2)  # members are 2-byte aligned


def read_control(deb_path):
    """Extract the control file text from a .deb."""
    data = deb_path.read_bytes()
    for name, body in ar_members(data):
        if name.startswith("control.tar"):
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
                for member in tar.getmembers():
                    if member.name.lstrip("./") == "control":
                        return tar.extractfile(member).read().decode("utf-8")
    raise ValueError(f"{deb_path.name}: no control file found")


def control_fields(control_text):
    fields = {}
    for line in control_text.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue  # continuation lines of multi-line fields
        key, value = line.split(":", 1)
        fields[key] = value.strip()
    return fields


def version_key(version):
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def strip_fields(control_text, names):
    """Drop the given fields, continuation lines included."""
    out, skipping = [], False
    for line in control_text.splitlines():
        if line[:1].isspace():
            if not skipping:
                out.append(line)
            continue
        skipping = ":" in line and line.split(":", 1)[0] in names
        if not skipping:
            out.append(line)
    return "\n".join(out)


def package_stanza(deb_path, control_text, package, repo_url):
    data = deb_path.read_bytes()
    # Depictions are rewritten rather than merely filled in: make_deb.sh bakes a
    # repo URL into the control at build time, so debs already sitting in old
    # releases point at wherever the repo lived back then. Owning the field here
    # keeps every package's page correct without rebuilding anything, and lets
    # the app repos stop caring where they are published.
    stanza = strip_fields(control_text.strip(), {"Depiction", "SileoDepiction"})
    stanza += f"\nDepiction: {repo_url}"
    stanza += f"\nSileoDepiction: {repo_url}depictions/{package}.json"
    stanza += f"\nFilename: debs/{deb_path.name}"
    stanza += f"\nSize: {len(data)}"
    stanza += f"\nMD5sum: {hashlib.md5(data).hexdigest()}"
    stanza += f"\nSHA1: {hashlib.sha1(data).hexdigest()}"
    stanza += f"\nSHA256: {hashlib.sha256(data).hexdigest()}"
    return stanza + "\n"


def release_file(repo, out_dir, index_names):
    lines = [
        f"Origin: {repo['label']}",
        f"Label: {repo['label']}",
        "Suite: stable",
        "Version: 1.0",
        "Codename: ios",
        f"Architectures: {ARCHITECTURES}",
        "Components: main",
        f"Description: {repo['description']}",
    ]
    for field, algo in (("MD5Sum", "md5"), ("SHA256", "sha256")):
        lines.append(f"{field}:")
        for name in index_names:
            data = (out_dir / name).read_bytes()
            digest = hashlib.new(algo, data).hexdigest()
            lines.append(f" {digest} {len(data)} {name}")
    return "\n".join(lines) + "\n"


def depiction_json(app, repo, screenshot_names, changelog):
    """Native Sileo depiction (also rendered by Zebra)."""
    details = []
    if app.get("tagline"):
        details.append({"class": "DepictionSubheaderView", "title": app["tagline"]})
    if app.get("description"):
        details.append({"class": "DepictionMarkdownView", "markdown": app["description"]})
    if screenshot_names:
        details.append(
            {
                "class": "DepictionScreenshotsView",
                "itemCornerRadius": 8,
                "itemSize": "{160, 348}",
                "screenshots": [
                    {
                        "url": f"{repo['url']}screenshots/{app['screenshots']}/{name}",
                        "accessibilityText": Path(name).stem,
                    }
                    for name in screenshot_names
                ],
            }
        )
    if app.get("github"):
        details.append(
            {
                "class": "DepictionTableButtonView",
                "title": "GitHub",
                "action": app["github"],
                "openExternal": True,
            }
        )

    log = []
    for entry in changelog:
        log.append(
            {"class": "DepictionSubheaderView", "useBoldText": True, "title": entry["tag"]}
        )
        log.append(
            {
                "class": "DepictionMarkdownView",
                "markdown": entry["body"] or "No notes for this release.",
            }
        )
    if not log:
        target = f"{app['github']}/releases" if app.get("github") else "the project page"
        log = [{"class": "DepictionMarkdownView", "markdown": f"See [releases]({target})."}]

    tabs = [{"tabname": "Changelog", "class": "DepictionStackView", "views": log}]
    if details:
        tabs.insert(0, {"tabname": "Details", "class": "DepictionStackView", "views": details})
    return {
        "minVersion": "0.1",
        "class": "DepictionTabView",
        "tintColor": repo.get("tint", "#ff0000"),
        "tabs": tabs,
    }


def load_changelog(path):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    entries.sort(key=lambda entry: version_key(entry["tag"]), reverse=True)
    return entries


def app_section_html(app, package, entries):
    """entries: list of (version, arch, filename) for this package, newest first."""
    by_version = {}
    for version, arch, filename in entries:
        by_version.setdefault(version, []).append((arch, filename))
    items = []
    for version, debs in by_version.items():
        links = " · ".join(
            f'<a href="debs/{filename}">{ARCH_LABELS.get(arch, arch)}</a>'
            for arch, filename in sorted(debs)
        )
        items.append(f"<li><b>{version}</b> — {links}</li>")
    title = app.get("name", package)
    tagline = f"<p>{app['tagline']}</p>" if app.get("tagline") else ""
    return f"""<h2>{title}</h2>
{tagline}
<ul>
{chr(10).join(items)}
</ul>"""


def index_html(repo, sections):
    url = repo["url"]
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{repo['label']} Repo</title>
<style>
 body {{ font-family: -apple-system, sans-serif; max-width: 600px;
        margin: 40px auto; padding: 0 16px; }}
 code {{ background: #eee; padding: 2px 6px; border-radius: 4px; }}
 a.btn {{ display: inline-block; margin: 4px 8px 4px 0; padding: 10px 16px;
         background: #d00; color: #fff; border-radius: 8px;
         text-decoration: none; }}
 h2 {{ margin-top: 32px; }}
</style>
</head>
<body>
<h1>{repo['label']} Repo</h1>
<p>{repo['description']}.</p>
<p>Add <code>{url}</code> to your package manager:</p>
<p>
<a class="btn" href="sileo://source/{url}">Add to Sileo</a>
<a class="btn" href="zbra://sources/add/{url}">Add to Zebra</a>
<a class="btn" href="cydia://url/https://cydia.saurik.com/api/share#?source={url}">Add to Cydia</a>
</p>
<p>Rootful (<code>iphoneos-arm</code>) and rootless (<code>iphoneos-arm64</code>)
packages are provided. Any version below can be installed from the repo
(Sileo/Zebra: package page → version list) or downloaded directly.</p>
{chr(10).join(sections)}
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="apps.toml")
    parser.add_argument("--debs", required=True, type=Path, help="directory with .deb files")
    parser.add_argument("--out", required=True, type=Path, help="output directory for the repo")
    parser.add_argument("--screenshots", type=Path, help="directory of per-app image folders")
    parser.add_argument("--changelogs", type=Path, help="directory of <package>.jsonl files")
    args = parser.parse_args()

    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    repo = config["repo"]
    apps = {app["package"]: app for app in config.get("apps", [])}

    debs = sorted(args.debs.rglob("*.deb"))
    if not debs:
        raise SystemExit(f"no .deb files in {args.debs}")

    # Newest versions first, so both Packages and the index read top-down.
    parsed = [(deb, text, control_fields(text)) for deb, text in ((d, read_control(d)) for d in debs)]
    parsed.sort(key=lambda item: (version_key(item[2]["Version"]), item[2]["Architecture"]))
    parsed.reverse()

    debs_out = args.out / "debs"
    debs_out.mkdir(parents=True, exist_ok=True)
    for deb, _, _ in parsed:
        shutil.copy2(deb, debs_out / deb.name)

    packages = "\n".join(
        package_stanza(deb, text, fields["Package"], repo["url"]) for deb, text, fields in parsed
    )
    packages_bytes = packages.encode("utf-8")
    (args.out / "Packages").write_bytes(packages_bytes)
    # mtime=0 keeps the .gz byte-identical across runs for the same input
    (args.out / "Packages.gz").write_bytes(gzip.compress(packages_bytes, mtime=0))
    (args.out / "Packages.bz2").write_bytes(bz2.compress(packages_bytes))
    (args.out / "Packages.xz").write_bytes(lzma.compress(packages_bytes))

    index_names = ["Packages", "Packages.gz", "Packages.bz2", "Packages.xz"]
    (args.out / "Release").write_text(release_file(repo, args.out, index_names))

    by_package = {}
    for deb, _, fields in parsed:
        entry = (fields["Version"], fields["Architecture"], deb.name)
        by_package.setdefault(fields["Package"], []).append(entry)

    depictions_out = args.out / "depictions"
    depictions_out.mkdir(exist_ok=True)
    sections = []
    for package, entries in by_package.items():
        app = apps.get(package, {"package": package})

        shot_names = []
        if args.screenshots and app.get("screenshots"):
            source = args.screenshots / app["screenshots"]
            if source.is_dir():
                target = args.out / "screenshots" / app["screenshots"]
                target.mkdir(parents=True, exist_ok=True)
                for image in sorted(source.iterdir()):
                    if image.suffix.lower() in (".jpeg", ".jpg", ".png"):
                        shutil.copy2(image, target / image.name)
                        shot_names.append(image.name)

        changelog = []
        if args.changelogs:
            path = args.changelogs / f"{package}.jsonl"
            if path.is_file():
                changelog = load_changelog(path)

        depiction = depiction_json(app, repo, shot_names, changelog)
        (depictions_out / f"{package}.json").write_text(json.dumps(depiction, indent=1))
        sections.append(app_section_html(app, package, entries))

    (args.out / "index.html").write_text(index_html(repo, sections))

    unknown = sorted(set(by_package) - set(apps))
    if unknown:
        print(f"note: no apps.toml entry for {', '.join(unknown)}")
    print(f"Repo written to {args.out} ({len(debs)} deb(s), {len(by_package)} package(s))")


if __name__ == "__main__":
    main()
