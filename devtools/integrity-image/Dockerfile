# syntax=docker/dockerfile:1
#
# OpenCloud image of the upload-integrity fork, for use with opencloud-compose
# (OC_DOCKER_IMAGE / OC_DOCKER_TAG). Build it with ./build.sh, see README.md.
#
# It is upstream's production image (opencloud/docker/Dockerfile.multiarch), with:
# - the server built from this checkout, i.e. with the vendored reva fork,
# - the web client built from the web fork instead of downloading the upstream release.
# Nothing in the checkout is modified; all steps run inside the build.

ARG WEB_REPO=https://github.com/Mortimer-RR/web.git
ARG WEB_REF=integrity

# --- web client from the web fork
FROM node:24 AS web
ARG WEB_REPO
ARG WEB_REF
WORKDIR /web
RUN git clone --depth 1 --branch "${WEB_REF}" "${WEB_REPO}" . && git log -1 --format='web: %H %s'
RUN --mount=type=cache,target=/pnpm-store \
    corepack enable && npm_config_store_dir=/pnpm-store pnpm install --frozen-lockfile && pnpm build

# --- idp assets, as `make -C services/idp assets` does upstream
FROM node:24 AS idp
WORKDIR /opencloud/services/idp
# the Makefile includes the shared make files
COPY .make /opencloud/.make
COPY services/idp/ ./
RUN --mount=type=cache,target=/pnpm-store \
    corepack enable && npm_config_store_dir=/pnpm-store make assets

# --- server binary, as upstream's Dockerfile.multiarch builds it
FROM quay.io/opencloudeu/golang-ci:1.26 AS build
ARG TARGETOS
ARG TARGETARCH
ARG VERSION
ARG STRING
ARG EDITION="rolling"
WORKDIR /opencloud
COPY . .
RUN rm -rf services/web/assets/core services/idp/assets
COPY --from=web /web/dist services/web/assets/core
COPY --from=idp /opencloud/services/idp/assets services/idp/assets
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache \
    GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH:-amd64}" ; \
    make -C opencloud release-linux-docker-${TARGETARCH:-amd64} \
        ENABLE_VIPS=true DIST=/dist \
        VERSION="${VERSION}" EDITION="${EDITION}" \
        ${STRING:+STRING="${STRING}"}

# --- runtime: unchanged from opencloud/docker/Dockerfile.multiarch
FROM alpine:3.24
ARG VERSION
ARG REVISION
ARG TARGETOS
ARG TARGETARCH

RUN apk add --no-cache attr bash ca-certificates curl imagemagick \
                       inotify-tools libc6-compat mailcap tree vips \
		       vips-magick patch && \
    echo 'hosts: files dns' >| /etc/nsswitch.conf

LABEL maintainer="openCloud GmbH <devops@opencloud.eu>" \
  org.opencontainers.image.title="OpenCloud (upload-integrity fork)" \
  org.opencontainers.image.vendor="OpenCloud GmbH" \
  org.opencontainers.image.authors="OpenCloud GmbH" \
  org.opencontainers.image.description="OpenCloud with the upload-integrity fixes, see FORK_NOTES.md" \
  org.opencontainers.image.licenses="Apache-2.0" \
  org.opencontainers.image.documentation="https://github.com/opencloud-eu/opencloud" \
  org.opencontainers.image.source="https://github.com/Mortimer-RR/opencloud" \
  org.opencontainers.image.version="${VERSION}" \
  org.opencontainers.image.revision="${REVISION}"

RUN addgroup -g 1000 -S opencloud-group && \
  adduser -S --ingroup opencloud-group --uid 1000 opencloud-user --home /var/lib/opencloud

RUN mkdir -p /var/lib/opencloud && \
# Pre-create the web directory to avoid permission issues
 mkdir -p /var/lib/opencloud/web/assets/apps && \
 chown -R opencloud-user:opencloud-group /var/lib/opencloud && \
 chmod -R 751 /var/lib/opencloud && \
 mkdir -p /etc/opencloud && \
 chown -R opencloud-user:opencloud-group /etc/opencloud && \
 chmod -R 751 /etc/opencloud

VOLUME [ "/var/lib/opencloud", "/etc/opencloud" ]
WORKDIR /var/lib/opencloud

USER 1000

EXPOSE 9200/tcp

ENTRYPOINT ["/usr/bin/opencloud"]
CMD ["server"]

COPY --from=build /dist/binaries/opencloud-linux-${TARGETARCH:-amd64} /usr/bin/opencloud
