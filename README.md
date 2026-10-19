# Kresus + Woob (master) + Sure bridge, in Docker

An image that bundles:

| Component | Source |
|---|---|
| **Kresus** | npm release pinned in the Dockerfile (`0.26.0`, build-arg `KRESUS_VERSION`) |
| **Woob** | https://gitlab.com/woob/woob at the master commit the patches were made against (`3bc0f159c`, build-arg `WOOB_REF`: branch, tag or full SHA) |
| **Patched modules** | `boursedirect`, `esalia`, `fortuneo`: `patches/0001-*.patch` applied on top of master |
| **`bp` module** | the master one, with what its Selenium login needs: Firefox ESR, geckodriver, `selenium` |
| **Sure bridge** | `sure-bridge/sure-bridge.py`, run every day by the `sure-bridge` service |

Kresus uses the modules of the embedded Woob checkout directly (`KRESUS_WOOB_SOURCES_LIST` points to
`file:///opt/woob/modules`): it does not download a module release.

## Getting started

```bash
cp config/sure-bridge.env.example config/sure-bridge.env   # then edit: Sure API key, account names
docker compose up -d --build
```

Kresus UI: http://localhost:9876 (add your bank accesses).

| Service | Role | Persistence |
|---|---|---|
| `kresus` | Kresus + Woob | volume `kresus-data` (`/var/lib/kresus`): SQLite database, `config.ini` with the encryption salt, sessions, module exports |
| `sure-bridge` | `sure-bridge.py sync` at `SURE_SYNC_TIME` (07:30 by default) | volume `sure-bridge-state` (what has already been sent); configuration in `config/sure-bridge.env` (outside the image) |

Both volumes survive `docker compose down` and image rebuilds. **`docker compose down -v` deletes them**:
back them up first (see below).

## Sure bridge configuration

Edit `config/sure-bridge.env` on the host, then `docker compose restart sure-bridge`. `KRESUS_URL` is
`http://kresus:9876` (the service name). Optional variables in `.env` (template: `.env.example`):
`TZ`, `KRESUS_PORT`, `SURE_SYNC_TIME` (HH:MM), `SURE_RUN_ON_START=1` (also send at startup).

```bash
docker compose run --rm sure-bridge sure-bridge.py accounts         # check the account names
docker compose run --rm sure-bridge sure-bridge.py sync --dry-run   # dry run
docker compose logs -f sure-bridge                                  # output of the sends
```

The Sure accounts must exist before the first send. See `sure-bridge/README.md`.

## Updating

The `Dockerfile` pins the Kresus version, the Woob commit and the Selenium and geckodriver versions, so the patches
keep applying. The base image (`node:24-bookworm-slim`) is deliberately not pinned: a rebuild picks up its latest
security updates. To upgrade:

```bash
docker compose build --build-arg KRESUS_VERSION=latest --build-arg WOOB_REF=master   # try the newest of both
```

then, if it works, put the new values in the `Dockerfile` and commit. The patches are applied with `git apply`:
if Woob changed the same lines, **the build fails** (it never produces a half-patched image). Regenerate the
patches from your local clone, which must be on the commit you want to build:

```bash
scripts/make-patches.sh /path/to/woob feat/boursedirect-esalia
```

## Image size

Multi-stage build on `node:24-bookworm-slim`: compilers, `-dev` packages, git and npm caches stay in the builder
stage, and npm/yarn, the icon theme and the docs are removed from the final image. About 1.3 GB uncompressed (a
single-stage build on the full `node` image was 3.1 GB). Firefox ESR, which the `bp` module's Selenium login needs,
is about 0.5 GB of it with its libraries, so there is no browser-less variant.

## Backup

```bash
docker run --rm -v kresus-docker_kresus-data:/d -v "$PWD":/b alpine tar czf /b/kresus-data.tgz -C /d .
```

(Volumes are prefixed with the compose project name `kresus-docker`: see `docker volume ls`.) The salt in `config.ini`
encrypts the bank credentials: without it the backed-up database is unusable.

## Good to know

- **Selenium and Firefox**: Woob still calls `find_element_by_xpath` (removed in Selenium 4.3), `capabilities=` and
  `service_log_path=` (removed in 4.10), so the image pins `selenium==4.2.0` (`SELENIUM_VERSION`). With a newer
  version the `bp` Selenium login fails (`AttributeError`) and the module falls back to the "legacy" HTTP browser:
  strong authentication at every login, and a 403 from the bank with Woob's frozen Firefox 102 User-Agent.
  Firefox runs headless with `shm_size: 1gb`.
- **Hard-coded paths**: the `boursedirect` and `esalia` modules write to `/var/lib/kresus` (Esalia tokens, JSON
  positions, device id). That is the data volume; the bridge reads the positions read-only.
- **Esalia**: the module registers the terminal (`REMEMBER_DEVICE = True`) with an identifier stored in
  `/var/lib/kresus/esalia-device-id`, i.e. in the volume. If the OTP code is asked again every day, look in
  `docker compose logs kresus` for the line "new device identifier generated": it means the identifier was
  recreated (volume lost or not mounted). If the identifier is stable and the code is still asked, set
  `ESALIA_KEEP_COOKIES=1` in `.env` and run `docker compose up -d`: the module then keeps its cookies between
  full logins (they are cleared by default), in case Esalia recognizes the trusted terminal through a cookie.
  Enter the OTP code once, then check the 03:00 sync the next day. Experimental, off by default.
- **Bourse Direct**: the module sets a 60 s HTTP timeout (Woob's default is 10 s) because the legacy `/priv/` pages
  can be slow right after a fresh login.
- **Tested**: build, startup, Kresus health, loading of the `bp`, `fortuneo`, `boursedirect` and `esalia` modules,
  Firefox + geckodriver + Selenium headless, bridge reaching Sure.

## Licenses

Kresus: AGPL-3.0. Woob and its modules: LGPL-3.0-or-later (the patches are modifications of them). The rest: personal use.
