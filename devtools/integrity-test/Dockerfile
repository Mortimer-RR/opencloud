# Runtime image for the integrity test. The binary is built by build.sh.
FROM alpine:3.24
RUN apk add --no-cache attr bash ca-certificates curl inotify-tools mailcap tree && \
    echo 'hosts: files dns' >| /etc/nsswitch.conf
COPY opencloud /usr/bin/opencloud
ENTRYPOINT ["/usr/bin/opencloud"]
CMD ["server"]
