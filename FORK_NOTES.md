# Fork notes: upload-integrity fork of OpenCloud

This fork exists only to build OpenCloud with the upload-integrity fixes until upstream
ships equivalent changes. The actual fixes live in the forked repos:

| Repo | Fork | Branch | What |
| --- | --- | --- | --- |
| reva | https://github.com/Mortimer-RR/reva | `integrity` | server fixes 1–4 (see its FORK_NOTES.md) |
| web | https://github.com/Mortimer-RR/web | `integrity` | fix 5: whole-file SHA1 on uploads |
| desktop | https://github.com/Mortimer-RR/desktop | `integrity` | fix 4: recover stale TUS uploads |

- Upstream: https://github.com/opencloud-eu/opencloud
- Base: `a1982c6908` (upstream `main` at the time of the audit)
- Tag fork releases `<upstream version>-integrity.N`.
- Before deploying a rebase onto a new upstream release, drop superseded fork commits
  and re-run the full torture test (`devtools/integrity-test/`).

## Divergences from upstream

### `go.mod`: reva replaced by the fork

```
replace github.com/opencloud-eu/reva/v2 => github.com/Mortimer-RR/reva/v2 <pseudo-version of reva integrity>
```

`vendor/` is regenerated from that. **Never hand-edit `vendor/`.** To pick up a new reva
fork commit:

```sh
# push the reva integrity branch first, then:
go mod edit -replace github.com/opencloud-eu/reva/v2=github.com/Mortimer-RR/reva/v2@<commit sha>
go mod tidy && go mod vendor
go build ./...
```

The fork keeps reva's module path (`github.com/opencloud-eu/reva/v2`), so Go gives it a
`v2.0.0-<date>-<sha>` pseudo-version. That is expected.

When upstream reva has merged the fixes and opencloud bumps to it, delete the
`replace` line and re-vendor.

Fixes that came in through the reva update also pull in
`github.com/tus/tusd/v2/pkg/memorylocker` (new in `vendor/`).

## Building the image for opencloud-compose

`devtools/integrity-image/build.sh` builds `opencloud-integrity:integrity-<commit>`:
upstream's production image (same runtime stage) with the server from this checkout and
the web client from the web fork. `devtools/integrity-image/smoke.sh` checks an image
with opencloud-compose's `docker-compose.yml`. See `devtools/integrity-image/README.md`.
Use it with opencloud-compose via `OC_DOCKER_IMAGE` / `OC_DOCKER_TAG`.

## Web assets (manual build without Docker)

The server embeds the web UI from `services/web/assets` (`//go:embed all:assets` in
`services/web/web.go`). The Makefile fills that directory from **upstream** web:

- `make -C services/web node-generate-prod` downloads the `web.tar.gz` release asset
  of `WEB_ASSETS_VERSION` (currently `v8.0.0`) from `opencloud-eu/web`.
- `make -C services/web node-generate-dev` clones `opencloud-eu/web` `main` and builds it.

Neither picks up the fork, and the Makefile is deliberately left unchanged. To build the
server with the forked web client:

```sh
git clone -b integrity git@github.com:Mortimer-RR/web.git /tmp/web-fork
make -C /tmp/web-fork release            # produces release/web.tar.gz
git -C services/web clean -xfd assets
tar xzf /tmp/web-fork/release/web.tar.gz -C services/web/assets/core/
make -C opencloud build                  # or the usual docker build
```

The web fork is based on web `main` (`ea72e3168c`), which is what opencloud `main`
pairs with in dev mode, not on the `v8.0.0` release.

## Deployment notes for the owner

- Keep `STORAGE_USERS_POSIX_ENABLE_FS_REVISIONS` off.
- Keep NATS (9233) and the storage-users data server (9158) bound to localhost or a
  private network. NATS has no authentication by default.
- Don't copy the bare-metal example's `admin/admin` or `OC_INSECURE=true`.
- Schedule `opencloud storage-users uploads sessions --clean` for expired uploads.
- Alert on sessions stuck in processing; recover them with `--resume`.
- Standardize on the desktop client for bulk syncing. It verifies checksums in both
  directions.
- Nothing re-verifies stored checksums at rest. Run the storage on ZFS or btrfs with
  scheduled scrubs.
- The tus locker is in-memory: run a single storage-users instance. Multiple replicas
  on shared storage would need tusd's `filelocker` (see reva FORK_NOTES, fix 1).
