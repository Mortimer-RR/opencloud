# OpenCloud image of the upload-integrity fork

Builds the `opencloud` image for
[opencloud-compose](https://github.com/opencloud-eu/opencloud-compose), with the
upload-integrity fixes:

- the server from this checkout, which vendors the reva fork (`go.mod` replace),
- the web client from the web fork (`https://github.com/Mortimer-RR/web.git`, branch
  `integrity`), instead of the upstream web release the normal build downloads.

The image is upstream's production image (`opencloud/docker/Dockerfile.multiarch`): same
runtime stage, non-root user `1000:1000`, vips, `/etc/opencloud` and `/var/lib/opencloud`
volumes, `EDITION=rolling`. Only the labels differ. Everything is built inside Docker. It
needs no local Go or Node toolchain and doesn't modify the checkout.

## Build

```sh
./devtools/integrity-image/build.sh
# -> opencloud-integrity:integrity-<opencloud commit>
```

| Variable | Default | |
| --- | --- | --- |
| `IMAGE` | `opencloud-integrity` | image name |
| `TAG` | `integrity-<commit>` | image tag, e.g. `8.0.1-integrity.1` for releases |
| `WEB_REPO` | `https://github.com/Mortimer-RR/web.git` | web fork to build |
| `WEB_REF` | `integrity` | branch or tag of the web fork |
| `VERSION` | empty | version string compiled into the binary (empty: `8.0.1+<commit>`) |
| `PLATFORM` | host | e.g. `linux/arm64` (needs buildx with emulation) |

The web client is cloned from `WEB_REPO`, so **push the web fork before building**. The
server is built from the checkout as it is (uncommitted changes included, with a warning).
`.dockerignore` excludes `.git`, so the script passes the commit in as build arguments.

## Use with opencloud-compose

In opencloud-compose's `.env`:

```sh
OC_DOCKER_IMAGE=opencloud-integrity
OC_DOCKER_TAG=integrity-<commit>
```

To run the image on another machine, push it to a registry and use the registry path as
`OC_DOCKER_IMAGE`, or move it with `docker save` / `docker load`.

## Smoke test

```sh
COMPOSE_DIR=/path/to/opencloud-compose ./devtools/integrity-image/smoke.sh opencloud-integrity:<tag>
```

Starts the image with opencloud-compose's `docker-compose.yml` as a separate compose project
(`integrity-smoke`, own volumes, removed afterwards) and checks that:

- `status.php` answers and the server runs as uid 1000,
- the admin can use WebDAV,
- a tus upload with a wrong checksum is rejected with 460 (fix 4), and one with the right
  checksum is stored and reads back,
- the embedded web client contains the upload checksum plugin (fix 5).

It enables basic auth with a random admin password for the test only, and leaves the
opencloud-compose checkout untouched.

The deeper verification is the torture test in `devtools/integrity-test/`.
