# APT repo

The Cydia/Sileo repository behind <https://verback2308.github.io/repo/>.

Packages arrive two ways, both described in `apps.toml`:

- entries with a `source` are pulled from that GitHub repository's releases on
  every run, so publishing a release there is all it takes to ship an update;
- entries without one are third-party packages we redistribute — commit the
  `.deb` under `debs/` and it gets indexed.

`apps.toml` also carries what each package's Sileo page says: tagline,
description, screenshots folder and GitHub link. The depiction URL baked into a
`.deb` at build time is ignored and rewritten here, so app repos do not need to
know where they are published.

## Local run

```bash
python3 scripts/make_repo.py --config apps.toml --debs debs --out site \
  --screenshots screenshots --changelogs changelogs
python3 scripts/test_make_repo.py
```

## Setup notes

- Settings → Pages → Source must be **GitHub Actions**.
- There is no custom domain, so **this repository's name is part of the URL
  every subscriber holds**. Renaming it silently drops all of them, the same way
  the rebrand dropped the previous ones. It is frozen, and lowercase on purpose:
  Sileo lowercases the URL a user types, and Pages paths are case-sensitive.
