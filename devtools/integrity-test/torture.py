#!/usr/bin/env python3
"""Upload-integrity torture test for the OpenCloud fork.

Drives uploads through the real stack (proxy, ocdav, datagateway, storage-users with the
posix driver and async postprocessing) and checks that every file either matches its source
sha256 exactly or failed visibly. A file that exists with a wrong hash is a failure.

Runs inside the `client` container (see run.sh). Stdlib only.

    python3 torture.py [scenario ...]
"""

import base64
import hashlib
import http.client
import json
import os
import random
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

OC_URL = os.environ.get("OC_URL", "https://opencloud.test:9200")
USER = "admin"
PASSWORD = os.environ["OC_ADMIN_PASSWORD"]
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
TLS = ssl._create_unverified_context()  # self-signed test server
CORPUS = "/corpus"
RESULTS = "/test/results"
CONTAINER = "integrity-opencloud-1"
COMPOSE = ["docker", "compose", "-p", "integrity", "--project-directory", "/test", "-f", "/test/docker-compose.yml"]

MiB = 1024 * 1024
GiB = 1024 * MiB

print_lock = threading.Lock()


def log(*args):
    with print_lock:
        print(time.strftime("%H:%M:%S"), *args, flush=True)


# --------------------------------------------------------------------------- HTTP


class Endpoint:
    """Where requests go: the server directly, or a proxy in front of it."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    def url(self, path):
        return self.base + path

    def dav(self, remote_path):
        return self.url(f"/remote.php/dav/files/{USER}/" + urllib.parse.quote(remote_path.lstrip("/")))


DIRECT = Endpoint(OC_URL)


def connect(url, timeout=600):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=TLS)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def target(url):
    u = urllib.parse.urlsplit(url)
    return u.path + (("?" + u.query) if u.query else "")


def request(method, url, headers=None, body=None, timeout=600, read=True):
    """Returns (status, headers, body). body may be bytes or a file-like object."""
    h = {"Authorization": AUTH}
    h.update(headers or {})
    conn = connect(url, timeout)
    try:
        conn.request(method, target(url), body=body, headers=h)
        res = conn.getresponse()
        data = res.read() if read else b""
        return res.status, {k.lower(): v for k, v in res.getheaders()}, data
    finally:
        conn.close()


class Slice:
    """File-like view of length bytes of a file starting at offset, for request bodies."""

    def __init__(self, path, offset, length):
        self.f = open(path, "rb")
        self.f.seek(offset)
        self.left = length

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        data = self.f.read(n)
        self.left -= len(data)
        return data

    def close(self):
        self.f.close()


# --------------------------------------------------------------------------- corpus


@dataclass
class CorpusFile:
    name: str
    path: str
    size: int
    sha256: str
    sha1: str
    mtime: int


def hash_file(path):
    s256, s1 = hashlib.sha256(), hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(8 * MiB):
            s256.update(chunk)
            s1.update(chunk)
    return s256.hexdigest(), s1.hexdigest()


def write_random(path, size, seed=None):
    rnd = random.Random(seed)
    with open(path, "wb") as f:
        left = size
        while left > 0:
            n = min(left, 8 * MiB)
            f.write(rnd.randbytes(n) if seed is not None else os.urandom(n))
            left -= n


def chunk_size():
    status, _, body = request("GET", DIRECT.url("/ocs/v1.php/cloud/capabilities?format=json"))
    caps = json.loads(body)["ocs"]["data"]["capabilities"]
    return int(caps["files"]["tus_support"]["max_chunk_size"])


def build_corpus(names=None):
    """Random files of the sizes the handoff asks for, cached in the corpus volume."""
    cs = chunk_size()
    sizes = {
        "zero.bin": 0,
        "one.bin": 1,
        "chunk.bin": cs,
        "chunk-plus-1.bin": cs + 1,
        "100m.bin": 100 * MiB,
        "1g.bin": GiB,
        "5g.bin": 5 * GiB,
        # same name in different folders, same size and mtime, different content
        "twin-a/twin.bin": 3 * cs + 12345,
        "twin-b/twin.bin": 3 * cs + 12345,
    }
    manifest_path = os.path.join(CORPUS, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    files = {}
    for name, size in sizes.items():
        if names and name not in names:
            continue
        path = os.path.join(CORPUS, name)
        entry = manifest.get(name)
        if not entry or entry["size"] != size or not os.path.exists(path) or os.path.getsize(path) != size:
            log(f"corpus: generating {name} ({size} bytes)")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_random(path, size)
            os.utime(path, (1700000000, 1700000000))
            s256, s1 = hash_file(path)
            entry = {"size": size, "sha256": s256, "sha1": s1, "mtime": 1700000000}
            manifest[name] = entry
            json.dump(manifest, open(manifest_path, "w"), indent=1)
        files[name] = CorpusFile(name, path, size, entry["sha256"], entry["sha1"], entry["mtime"])
    return files


def small_file(name, size, seed, mtime=1700000000):
    """A deterministic temporary file outside the cached corpus."""
    path = os.path.join("/tmp", name.replace("/", "_"))
    if not os.path.exists(path) or os.path.getsize(path) != size:
        write_random(path, size, seed)
    s256, s1 = hash_file(path)
    return CorpusFile(name, path, size, s256, s1, mtime)


# --------------------------------------------------------------------------- uploads


class UploadRejected(Exception):
    """The server refused the upload (a visible failure)."""


def mkcol_p(ep, remote_dir):
    parts = [p for p in remote_dir.strip("/").split("/") if p]
    for i in range(1, len(parts) + 1):
        request("MKCOL", ep.dav("/".join(parts[:i]) + "/"))


def tus_create(ep, remote, size, sha1=None, mtime=None, first_chunk=None):
    parent, name = os.path.split(remote.strip("/"))
    meta = [f"filename {base64.b64encode(name.encode()).decode()}"]
    if sha1:
        meta.append(f"checksum {base64.b64encode(f'sha1 {sha1}'.encode()).decode()}")
    if mtime is not None:
        meta.append(f"mtime {base64.b64encode(str(mtime).encode()).decode()}")
    headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(size), "Upload-Metadata": ",".join(meta)}
    status, h, body = request("POST", ep.dav(parent + "/"), headers)
    if status != 201 or "location" not in h:
        raise UploadRejected(f"tus create {remote}: {status} {body[:200]!r}")
    return h["location"]


def tus_head(location):
    status, h, _ = request("HEAD", location, {"Tus-Resumable": "1.0.0"}, read=False)
    return status, int(h.get("upload-offset", -1))


def tus_patch(location, f: CorpusFile, offset, length, timeout=600):
    body = Slice(f.path, offset, length)
    try:
        return request(
            "PATCH",
            location,
            {
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(offset),
                "Content-Length": str(length),
            },
            body,
            timeout=timeout,
        )
    finally:
        body.close()


def tus_upload(ep, f: CorpusFile, remote, chunk, checksum=True, attempts=40, backoff=3, timeout=600):
    """Uploads like a well-behaved tus client: resumes from the server offset after errors.
    Raises UploadRejected for visible failures."""
    sha1 = f.sha1 if checksum else None
    location = None
    offset = 0
    for attempt in range(attempts):
        try:
            if location is None:
                location = tus_create(ep, remote, f.size, sha1, f.mtime)
                offset = 0
                if f.size == 0:
                    return
            else:
                status, server_offset = tus_head(location)
                if status in (404, 410):
                    location = None  # the upload is gone, start over
                    continue
                if status != 200:
                    raise IOError(f"HEAD {status}")
                offset = server_offset
                if offset == f.size:
                    return
            while offset < f.size:
                n = min(chunk, f.size - offset)
                status, h, body = tus_patch(location, f, offset, n, timeout)
                if status == 460:
                    raise UploadRejected(f"{remote}: checksum mismatch (460)")
                if status in (400, 412) and b"checksum" in body.lower():
                    raise UploadRejected(f"{remote}: {status} {body[:200]!r}")
                if status != 204:
                    raise IOError(f"PATCH {status} {body[:200]!r}")
                offset = int(h["upload-offset"])
            return
        except UploadRejected:
            raise
        except Exception as e:  # network errors, server restarts, 5xx: resume
            if attempt == attempts - 1:
                raise UploadRejected(f"{remote}: gave up after {attempts} attempts: {e}")
            time.sleep(backoff)
    raise UploadRejected(f"{remote}: gave up")


def put_upload(ep, f: CorpusFile, remote, checksum=True, timeout=1800):
    headers = {"Content-Length": str(f.size), "X-OC-Mtime": str(f.mtime)}
    if checksum:
        headers["OC-Checksum"] = f"SHA1:{f.sha1}"
    body = Slice(f.path, 0, f.size)
    try:
        status, _, data = request("PUT", ep.dav(remote), headers, body, timeout=timeout)
    finally:
        body.close()
    if status not in (200, 201, 204):
        raise UploadRejected(f"PUT {remote}: {status} {data[:200]!r}")


# --------------------------------------------------------------------------- verification

MATCHED, FAILED, CORRUPT, STUCK, MISSING = "matched", "failed-visibly", "corrupt", "stuck-processing", "missing"


def propfind(ep, remote, props, depth="0"):
    body = (
        '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
        + "".join(props)
        + "</d:prop></d:propfind>"
    )
    status, _, data = request("PROPFIND", ep.dav(remote), {"Depth": depth, "Content-Type": "application/xml"}, body.encode())
    return status, data


def stored_sha1(remote):
    status, data = propfind(DIRECT, remote, ["<oc:checksums/>"])
    if status != 207:
        return None
    for el in ET.fromstring(data).iter("{http://owncloud.org/ns}checksum"):
        for part in (el.text or "").split():
            if part.upper().startswith("SHA1:"):
                return part.split(":", 1)[1].lower()
    return None


def download_sha256(url, headers=None):
    conn = connect(url, timeout=1800)
    try:
        h = {"Authorization": AUTH}
        h.update(headers or {})
        conn.request("GET", target(url), headers=h)
        res = conn.getresponse()
        if res.status != 200:
            res.read()
            return res.status, None
        s = hashlib.sha256()
        while chunk := res.read(8 * MiB):
            s.update(chunk)
        return 200, s.hexdigest()
    finally:
        conn.close()


def verify(remote, f: CorpusFile, wait=900):
    """Downloads the file and compares sha256, and the stored SHA1 checksum.
    Waits while the file is still in postprocessing (425)."""
    deadline = time.time() + wait
    while True:
        status, sha = download_sha256(DIRECT.dav(remote))
        if status == 425 and time.time() < deadline:
            time.sleep(2)
            continue
        break
    if status == 404:
        return MISSING, "not found"
    if status == 425:
        return STUCK, "still processing"
    if status != 200:
        return CORRUPT, f"download failed with {status}"
    if sha != f.sha256:
        return CORRUPT, f"sha256 {sha} != {f.sha256}"
    stored = stored_sha1(remote)
    if stored is None:
        # the content is right; the server just reports no checksum (seen for 0-byte files)
        return MATCHED, "no stored checksum"
    if stored != f.sha1:
        return CORRUPT, f"stored SHA1 {stored} != {f.sha1}"
    return MATCHED, ""


# --------------------------------------------------------------------------- server control


def docker(*args, check=True, capture=True):
    return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True)


def wait_for_server(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = propfind(DIRECT, "/", ["<d:resourcetype/>"])
            if status == 207:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("server did not come up")


def kill_server():
    log("server: kill -9")
    docker("kill", "--signal", "KILL", CONTAINER, check=False)


def start_server():
    docker("start", CONTAINER, check=False)
    wait_for_server()
    log("server: up")


def recreate_server(*override_files, **env):
    """Recreates the server container, optionally with compose override files."""
    cmd = list(COMPOSE)
    for o in override_files:
        cmd += ["-f", f"/test/{o}"]
    cmd += ["up", "-d", "--force-recreate", "opencloud"]
    subprocess.run(cmd, check=True, capture_output=True, text=True, env={**os.environ, **env})
    wait_for_server()
    log("server: recreated", " ".join(override_files) or "(default config)")


def processing_sessions():
    r = docker("exec", CONTAINER, "opencloud", "storage-users", "uploads", "sessions", "--processing", "--json", check=False)
    start = r.stdout.find("[")
    if start < 0:
        return []
    sessions = json.loads(r.stdout[start:])
    return [s for s in sessions if s.get("status") == "processing" or s.get("processing")]


def resume_processing():
    docker("exec", CONTAINER, "opencloud", "storage-users", "uploads", "sessions", "--processing", "--resume", check=False)


def personal_space_id():
    status, _, body = request("GET", DIRECT.url("/graph/v1.0/me/drives"))
    for d in json.loads(body).get("value", []):
        if d.get("driveType") == "personal":
            return d["id"]
    raise RuntimeError("no personal space")


def cleanup(*remote_dirs):
    """Deletes a scenario's uploads and empties the trash, so runs don't fill the disk."""
    for d in remote_dirs:
        request("DELETE", DIRECT.dav(d.rstrip("/") + "/"))
    try:
        request("DELETE", DIRECT.url(f"/remote.php/dav/spaces/trash-bin/{personal_space_id()}/"))
    except Exception as e:
        log(f"cleanup: could not empty the trash: {e}")


SCENARIO_DIRS = {
    "baseline": ["/s1"], "netem": ["/s2"], "overlap": ["/s3"], "crash": ["/s4"], "proxy": ["/s5"],
    "restore": ["/s6"], "aborted": ["/s7"],
}


# --------------------------------------------------------------------------- results


@dataclass
class Outcome:
    remote: str
    file: CorpusFile
    upload_error: str = ""
    result: str = ""
    detail: str = ""
    retried: bool = False


@dataclass
class ScenarioResult:
    name: str
    outcomes: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    extra_counts: dict = field(default_factory=dict)  # for scenarios that count reads, not files

    def counts(self):
        if self.extra_counts:
            return self.extra_counts
        c = {"files": len(self.outcomes), MATCHED: 0, FAILED: 0, CORRUPT: 0}
        for o in self.outcomes:
            if o.result == CORRUPT:
                c[CORRUPT] += 1
            elif o.upload_error or o.retried:
                c[FAILED] += 1
            elif o.result == MATCHED:
                c[MATCHED] += 1
        return c


def settle(outcome: Outcome, retry_upload=None):
    """Verifies an outcome. A visibly failed upload must not have left a corrupt file; it is
    retried once under normal conditions and must then match."""
    result, detail = verify(outcome.remote, outcome.file)
    if outcome.upload_error:
        if result == MATCHED or result == MISSING:
            if retry_upload and result == MISSING:
                try:
                    retry_upload()
                    outcome.retried = True
                    result, detail = verify(outcome.remote, outcome.file)
                except UploadRejected as e:
                    result, detail = FAILED, f"retry failed: {e}"
            else:
                result = MATCHED if result == MATCHED else FAILED
        elif result == STUCK:
            pass
        else:
            result = CORRUPT
    outcome.result, outcome.detail = result, detail if result != MATCHED or detail else ""
    if result not in (MATCHED, FAILED):
        log(f"  !! {outcome.remote}: {result} {detail} (upload error: {outcome.upload_error})")
    return outcome


def run_uploads(name, jobs, workers=20, retry=True):
    """jobs: list of (remote, file, upload_fn). upload_fn() raises UploadRejected on failure."""
    res = ScenarioResult(name)

    def one(remote, f, fn):
        o = Outcome(remote, f)
        try:
            fn()
        except UploadRejected as e:
            o.upload_error = str(e)
        except Exception as e:
            o.upload_error = f"{type(e).__name__}: {e}"
        return o, fn

    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(one, *j) for j in jobs]
        done = [fut.result() for fut in as_completed(futures)]
    for o, fn in done:
        res.outcomes.append(settle(o, fn if retry else None))
    return res


# --------------------------------------------------------------------------- scenarios


def jobs_for(ep, files, prefix, methods=("tus", "put"), chunk=None, checksum=True):
    chunk = chunk or chunk_size()
    jobs = []
    for f in files.values():
        for m in methods:
            remote = f"{prefix}/{m}/{f.name}"
            mkcol_p(DIRECT, os.path.dirname(remote))
            if m == "tus":
                fn = lambda f=f, r=remote: tus_upload(ep, f, r, chunk, checksum)
            else:
                fn = lambda f=f, r=remote: put_upload(ep, f, r, checksum)
            jobs.append((remote, f, fn))
    return jobs


def scenario_baseline(files):
    """1. Parallel uploads (20 concurrent) via tus and plain WebDAV PUT."""
    return run_uploads("1 baseline", jobs_for(DIRECT, files, "/s1"), workers=20)


def scenario_netem(files):
    """2. 5% packet loss and 200 ms delay between client and server."""
    subset = {k: v for k, v in files.items() if v.size <= 100 * MiB}
    subprocess.run(["tc", "qdisc", "add", "dev", "eth0", "root", "netem", "loss", "5%", "delay", "200ms"], check=True)
    try:
        res = run_uploads("2 netem loss 5% delay 200ms", jobs_for(DIRECT, subset, "/s2"), workers=10, retry=False)
    finally:
        subprocess.run(["tc", "qdisc", "del", "dev", "eth0", "root"], check=False)
    # retries and verification happen without netem
    for o in res.outcomes:
        if o.upload_error and o.result == FAILED:
            settle(o, lambda o=o: tus_upload(DIRECT, o.file, o.remote, chunk_size()) if "/tus/" in o.remote else put_upload(DIRECT, o.file, o.remote))
    res.notes.append("1 GB and 5 GB files skipped: throughput under 5% loss + 200 ms makes them take hours")
    return res


def overlap_once(i, f: CorpusFile, checksum):
    """Starts a PATCH, and while it is still streaming does HEAD + PATCH from the returned
    offset, like a client retrying after a timeout. Then lets the first request continue."""
    remote = f"/s3/overlap-{i}.bin"
    o = Outcome(remote, f)
    try:
        location = tus_create(DIRECT, remote, f.size, f.sha1 if checksum else None, f.mtime)
        half = f.size // 2
        data = open(f.path, "rb").read()
        c1 = connect(location, timeout=60)
        c1.putrequest("PATCH", target(location))
        for k, v in {
            "Authorization": AUTH,
            "Tus-Resumable": "1.0.0",
            "Content-Type": "application/offset+octet-stream",
            "Upload-Offset": "0",
            "Content-Length": str(f.size),
        }.items():
            c1.putheader(k, v)
        c1.endheaders()
        c1.send(data[:half])
        # wait until the server has written (part of) the first half
        offset = 0
        for _ in range(50):
            time.sleep(0.02)
            status, offset = tus_head(location)
            if offset > 0:
                break
        o.detail = f"overlap at offset {offset}" if 0 < offset < f.size else ""
        # the retry: PATCH the next part from the server's offset (not all of it, so the upload
        # stays open while the first request continues)
        retry_end = offset + (f.size - offset) // 2
        status, h, _ = request(
            "PATCH",
            location,
            {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream", "Upload-Offset": str(offset)},
            data[offset:retry_end],
            timeout=60,
        )
        # the stalled first request wakes up and sends the rest of its body
        try:
            c1.send(data[half:])
            c1.getresponse().read()
        except Exception:
            pass
        finally:
            c1.close()
        if status == 460:
            raise UploadRejected("460")
        if status == 204 and int(h.get("upload-offset", -1)) == f.size:
            return o  # the retry completed the upload; the first request was interrupted
        # finish the upload like a client would
        status, offset = tus_head(location)
        if status == 200 and offset < f.size:
            st, _, _ = request(
                "PATCH",
                location,
                {"Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream", "Upload-Offset": str(offset)},
                data[offset:],
            )
            if st == 460:
                raise UploadRejected("460")
        # 404: the upload already finished (and its session is gone); the content check decides
    except UploadRejected as e:
        o.upload_error = str(e)
    except Exception as e:
        o.upload_error = f"{type(e).__name__}: {e}"
    return o


def scenario_overlap(files, iterations=1000):
    """3. Forced overlap: PATCH, and while it is streaming HEAD + PATCH again. Without checksum,
    like a browser upload today, so corruption would be silent."""
    f = small_file("overlap.bin", 4 * MiB + 7, seed=3)
    res = ScenarioResult("3 forced overlap x%d" % iterations)
    mkcol_p(DIRECT, "/s3")
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda i: overlap_once(i, f, checksum=False), range(iterations)))
    overlapped = sum(1 for o in outcomes if o.detail.startswith("overlap"))
    for o in outcomes:
        res.outcomes.append(settle(o))
    res.notes.append("uploads without checksum, so only the server's own consistency protects them")
    res.notes.append(f"{overlapped} of {iterations} retries started while the first PATCH had written part of its body")
    return res


def scenario_crash(files):
    """4. kill -9 the server mid-upload, restart, resume. Then kill it between upload completion
    and the end of postprocessing."""
    res = ScenarioResult("4 server crash")
    cs = chunk_size()
    targets = {k: v for k, v in files.items() if k in ("100m.bin", "1g.bin", "chunk-plus-1.bin", "twin-a/twin.bin")}
    mkcol_p(DIRECT, "/s4/a/twin-a")
    mkcol_p(DIRECT, "/s4/a/twin-b")

    def upload(f):
        o = Outcome(f"/s4/a/{f.name}", f)
        try:
            tus_upload(DIRECT, f, o.remote, cs, attempts=200, backoff=2)
        except UploadRejected as e:
            o.upload_error = str(e)
        return o

    # a) mid-upload
    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(upload, f) for f in targets.values()]
        time.sleep(4)
        kill_server()
        time.sleep(3)
        start_server()
        outcomes = [fut.result() for fut in futures]
    for o in outcomes:
        res.outcomes.append(settle(o, lambda o=o: tus_upload(DIRECT, o.file, o.remote, cs)))

    # b) between upload completion and the end of postprocessing
    recreate_server("delay.override.yml", POSTPROCESSING_DELAY="20s")
    mkcol_p(DIRECT, "/s4/b/twin-a")
    mkcol_p(DIRECT, "/s4/b/twin-b")
    small = {k: v for k, v in files.items() if v.size <= 100 * MiB}
    pending = []
    for f in small.values():
        o = Outcome(f"/s4/b/{f.name}", f)
        try:
            tus_upload(DIRECT, f, o.remote, cs)
        except UploadRejected as e:
            o.upload_error = str(e)
        pending.append(o)
    kill_server()
    time.sleep(3)
    start_server()
    stuck = processing_sessions()
    res.notes.append(f"{len(stuck)} upload sessions in processing after the crash, recovered with `uploads sessions --resume`")
    resume_processing()
    for o in pending:
        res.outcomes.append(settle(o, lambda o=o: tus_upload(DIRECT, o.file, o.remote, cs)))
    recreate_server()
    return res


def scenario_proxy(files):
    """5. nginx in front with request buffering on and a 5 s proxy_read_timeout."""
    ep = Endpoint("http://nginx")
    subset = {k: v for k, v in files.items() if v.size <= GiB}
    res = run_uploads("5 nginx buffering + short timeout", jobs_for(ep, subset, "/s5"), workers=20)
    return res


def dav_version_restore(fileid, version_href, remote):
    status, _, body = request(
        "COPY",
        DIRECT.url(version_href),
        {"Destination": DIRECT.dav(remote), "Overwrite": "T"},
    )
    if status not in (201, 204):
        raise RuntimeError(f"restore {status} {body[:200]!r}")


def versions(fileid):
    url = DIRECT.url(f"/remote.php/dav/meta/{urllib.parse.quote(fileid)}/v")
    body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/></d:prop></d:propfind>'
    status, _, data = request("PROPFIND", url, {"Depth": "1", "Content-Type": "application/xml"}, body.encode())
    hrefs = [el.text for el in ET.fromstring(data).iter("{DAV:}href")]
    return [h for h in hrefs if "/v/" in h]


def scenario_restore(files):
    """6. Restore versions of a 1 GB file while 10 readers download it in a loop. Every
    download must hash to either the old or the new version."""
    res = ScenarioResult("6 version restore under load")
    v1 = files["1g.bin"]
    # a later edit: different content and a later mtime. (Versions with the same mtime hit a
    # separate, pre-existing restore bug, see the report.)
    v2 = small_file("1g-v2.bin", GiB, seed=6, mtime=1700003600)
    remote = "/s6/big.bin"
    mkcol_p(DIRECT, "/s6")
    cs = chunk_size()
    tus_upload(DIRECT, v1, remote, cs)
    verify(remote, v1)
    tus_upload(DIRECT, v2, remote, cs)
    verify(remote, v2)
    status, data = propfind(DIRECT, remote, ["<oc:fileid/>"])
    fileid = next(ET.fromstring(data).iter("{http://owncloud.org/ns}fileid")).text

    good = {v1.sha256, v2.sha256}
    counts = {"files": 0, MATCHED: 0, FAILED: 0, CORRUPT: 0}
    lock = threading.Lock()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                status, sha = download_sha256(DIRECT.dav(remote))
            except Exception:
                status, sha = 0, None  # e.g. the server dropped the connection
            with lock:
                counts["files"] += 1
                if status != 200:
                    counts[FAILED] += 1
                elif sha in good:
                    counts[MATCHED] += 1
                else:
                    counts[CORRUPT] += 1
                    log(f"  !! download during restore hashed to {sha}")

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    restores = 0
    try:
        for _ in range(6):
            time.sleep(5)
            hrefs = versions(fileid)
            if not hrefs:
                break
            dav_version_restore(fileid, hrefs[0], remote)
            restores += 1
    finally:
        time.sleep(5)
        stop.set()
        for t in threads:
            t.join()
    res.extra_counts = counts
    res.notes.append(f"{restores} restores while 10 readers downloaded continuously; counts are downloads")
    # the final state must be one of the two versions
    status, sha = download_sha256(DIRECT.dav(remote))
    if sha not in good:
        counts[CORRUPT] += 1
        log(f"  !! final content after restores is neither version: {sha}")
    return res


def scenario_aborted(files):
    """7. Postprocessing of an older upload fails after a newer upload finalized. The newer
    content must survive, and the version the older upload created must still exist."""
    res = ScenarioResult("7 aborted postprocessing")
    cs = chunk_size()
    v0, va, vb = small_file("s7-v0.bin", 3 * MiB, 70), small_file("s7-va.bin", 4 * MiB, 71), small_file("s7-vb.bin", 5 * MiB, 72)
    remote = "/s7/file.bin"
    mkcol_p(DIRECT, "/s7")
    tus_upload(DIRECT, v0, remote, cs)
    verify(remote, v0)

    # upload A stays in postprocessing
    recreate_server("delay.override.yml", POSTPROCESSING_DELAY="1h")
    before = {s.get("id") or s.get("ID") for s in processing_sessions()}
    tus_upload(DIRECT, va, remote, cs)
    time.sleep(2)
    a_sessions = [s for s in processing_sessions() if (s.get("id") or s.get("ID")) not in before]
    if len(a_sessions) != 1:
        res.notes.append(f"could not identify upload A's session: {a_sessions}")
    a_id = a_sessions[0].get("id") or a_sessions[0].get("ID")

    # upload B finalizes
    recreate_server()
    tus_upload(DIRECT, vb, remote, cs)
    o = Outcome(remote, vb)
    settle(o)
    if o.result != MATCHED:
        res.notes.append(f"upload B did not finalize: {o.result} {o.detail}")

    # A's postprocessing is aborted now
    docker("exec", CONTAINER, "opencloud", "storage-users", "uploads", "sessions", "--id", a_id, "--clean")
    time.sleep(5)
    o = Outcome(remote, vb)
    res.outcomes.append(settle(o))
    status, data = propfind(DIRECT, remote, ["<oc:fileid/>"])
    fileid = next(ET.fromstring(data).iter("{http://owncloud.org/ns}fileid")).text
    res.notes.append(f"after aborting A: {len(versions(fileid))} versions kept (the version A created holds v0)")
    return res


NETWORK = "integrity_net"
DESKTOP_IMAGE = os.environ.get("DESKTOP_IMAGE", "desktop-build")
DESKTOP_BUILD_VOLUME = os.environ.get("DESKTOP_BUILD_VOLUME", "desktop-build-dir")
PLAYWRIGHT_IMAGE = os.environ.get("PLAYWRIGHT_IMAGE", "mcr.microsoft.com/playwright:v1.63.0-noble")


def scenario_desktop(files):
    """8. Desktop client sync through a proxy that forces a chunk timeout, drops connections and
    corrupts one upload. The corrupted upload must be rejected, then succeed on retry."""
    import socket

    import chaosproxy

    res = ScenarioResult("8 desktop client")
    run_id = time.strftime("%H%M%S")
    names = ["one.bin", "chunk-plus-1.bin", "100m.bin", "twin-a/twin.bin"]
    local = f"/desktop/{run_id}"
    for n in names:
        os.makedirs(os.path.dirname(f"{local}/{n}"), exist_ok=True)
        subprocess.run(["cp", files[n].path, f"{local}/{n}"], check=True)
    remote_root = f"/s8-{run_id}"
    mkcol_p(DIRECT, remote_root)

    ip = socket.gethostbyname(socket.gethostname())
    chaos = chaosproxy.Chaos(f"http://{ip}:8080", corrupt_name="chunk-plus-1.bin", resets=2, stall_seconds=330)
    srv = chaosproxy.start(chaos, 8080)
    # Like a real client, sync again later: items that failed are retried after the client's
    # error blacklist delay (5 s, then 25 s, ...). The proxy faults are one-shot.
    codes = []
    try:
        for round_ in range(4):
            if round_:
                time.sleep(30)
            r = docker(
                "run", "--rm", "--network", NETWORK,
                "-v", f"{DESKTOP_BUILD_VOLUME}:/build:ro",
                "-v", "integrity_desktop:/desktop",
                "-e", "QT_QPA_PLATFORM=offscreen",
                DESKTOP_IMAGE,
                "/build/bin/opencloudcmd", "--trust", "--non-interactive", "--max-sync-retries", "3",
                "-u", USER, "-t", PASSWORD, "--remote-folder", remote_root,
                f"http://{ip}:8080", "Personal", local,
                check=False,
            )
            codes.append(r.returncode)
            if all(download_sha256(DIRECT.dav(f"{remote_root}/{n}"))[0] == 200 for n in names):
                break
    finally:
        srv.shutdown()
    res.notes.append(f"{len(codes)} sync rounds, opencloudcmd exit codes {codes}")
    res.notes += [f"proxy: {e}" for e in chaos.events]
    rejected = any(st == 460 for _, _, st, _ in chaos.statuses)
    res.notes.append("the corrupted upload was rejected with 460" if rejected else "NO 460 seen for the corrupted upload")
    for n in names:
        o = Outcome(f"{remote_root}/{n}", files[n])
        res.outcomes.append(settle(o))
        if n == chaos.corrupt_name and rejected:
            o.retried = True  # failed visibly first, then succeeded on retry
    if not rejected:
        res.outcomes.append(Outcome("(corrupted upload)", files["chunk-plus-1.bin"], result=CORRUPT, detail="no 460"))
    return res


def run_web(mode, folder, paths):
    script = open("/test/web/upload.mjs").read()
    cmd = [
        "docker", "run", "--rm", "-i", "--network", NETWORK, "--ipc=host",
        "-v", "integrity_corpus:/corpus:ro", "-v", "integrity_pw:/pw", "-w", "/pw",
        "-e", f"OC_URL={OC_URL}", "-e", f"OC_ADMIN_PASSWORD={PASSWORD}",
        PLAYWRIGHT_IMAGE, "sh", "-c",
        '[ -d node_modules/playwright ] || npm i -s playwright@1.63.0 >/dev/null 2>&1; cat > upload.mjs; node upload.mjs "$@"', "sh",
        mode, folder, *paths,
    ]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True)
    out = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if r.returncode != 0 or not out:
        raise RuntimeError(f"web run failed ({r.returncode}): {r.stderr[-1500:]}")
    return json.loads(out[-1])


def scenario_web(files):
    """9. Headless browser upload of the corpus through the web UI, one upload with a tampered
    checksum, and the same-fingerprint resume splice."""
    res = ScenarioResult("9 web client")
    run_id = time.strftime("%H%M%S")

    # the corpus (the 5 GB file only once, it dominates the run time)
    folder = f"s9-{run_id}"
    mkcol_p(DIRECT, folder)
    names = [n for n in files if not n.startswith("twin-")]
    out = run_web("normal", folder, [f"/corpus/{n}" for n in names])
    res.notes.append(f"corpus upload: {out['title']}")
    for n in names:
        res.outcomes.append(settle(Outcome(f"{folder}/{os.path.basename(n)}", files[n])))

    # tampered checksum: must be rejected and leave no file behind
    folder_t = f"s9-{run_id}-tamper"
    mkcol_p(DIRECT, folder_t)
    out = run_web("tamper", folder_t, ["/corpus/chunk-plus-1.bin"])
    res.notes.append(f"tampered upload: {out['title']} {out['errors']}")
    o = Outcome(f"{folder_t}/chunk-plus-1.bin", files["chunk-plus-1.bin"], upload_error="tampered checksum")
    result, detail = verify(o.remote, o.file, wait=60)
    o.result = FAILED if result == MISSING else (CORRUPT if result != MATCHED else MATCHED)
    o.detail = detail
    if o.result == MATCHED:
        o.result, o.detail = CORRUPT, "an upload with a wrong checksum was accepted"
    res.outcomes.append(o)

    # same name, size and mtime, different content: the resume splice must not be stored
    folder_w = f"s9-{run_id}-twins"
    mkcol_p(DIRECT, folder_w)
    out = run_web("twins", folder_w, ["/corpus/twin-a/twin.bin", "/corpus/twin-b/twin.bin"])
    res.notes.append(f"twins: {out['title']} {out['errors']}")
    remote = f"{folder_w}/twin.bin"
    status, sha = download_sha256(DIRECT.dav(remote))
    a, b = files["twin-a/twin.bin"], files["twin-b/twin.bin"]
    o = Outcome(remote, b)
    if status == 404:
        o.result, o.upload_error = FAILED, "spliced upload rejected"
    elif sha == b.sha256:
        o.result = MATCHED
    elif sha == a.sha256:
        o.result, o.detail = CORRUPT, "the first file's content was stored for the second upload"
    else:
        o.result, o.detail = CORRUPT, f"spliced content stored: {sha}"
    res.outcomes.append(o)
    return res


SCENARIOS = {
    "baseline": scenario_baseline,
    "netem": scenario_netem,
    "overlap": scenario_overlap,
    "crash": scenario_crash,
    "proxy": scenario_proxy,
    "restore": scenario_restore,
    "aborted": scenario_aborted,
    "desktop": scenario_desktop,
    "web": scenario_web,
}


def list_root():
    status, data = propfind(DIRECT, "/", ["<d:resourcetype/>"], depth="1")
    names = []
    for href in ET.fromstring(data).iter("{DAV:}href"):
        name = urllib.parse.unquote(href.text.rstrip("/").rsplit("/", 1)[-1])
        if name and name != USER:
            names.append(name)
    return names


def report(results):
    lines = [
        "| Scenario | Files | Matched | Failed visibly | Corrupt (must be 0) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        c = r.counts()
        lines.append(f"| {r.name} | {c['files']} | {c[MATCHED]} | {c[FAILED]} | {c[CORRUPT]} |")
    lines.append("")
    for r in results:
        for n in r.notes:
            lines.append(f"- {r.name}: {n}")
        for o in r.outcomes:
            if o.result not in (MATCHED, FAILED) or o.upload_error or o.detail:
                lines.append(f"  - {o.remote}: {o.result} {o.detail} {('(upload: ' + o.upload_error + ')') if o.upload_error else ''}")
    stuck = processing_sessions()
    lines.append(f"\nUpload sessions still in processing at the end: {len(stuck)}")
    return "\n".join(lines)


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    wait_for_server()
    files = build_corpus()
    os.makedirs(RESULTS, exist_ok=True)
    results = []
    for n in names:
        log(f"=== scenario {n}")
        r = SCENARIOS[n](files)
        results.append(r)
        log(f"=== {r.name}: {r.counts()}")
        if os.environ.get("KEEP_UPLOADS") != "1":
            dirs = SCENARIO_DIRS.get(n) or [d for d in list_root() if d.startswith(("s8-", "s9-"))]
            cleanup(*dirs)
    text = report(results)
    print(text)
    with open(os.path.join(RESULTS, time.strftime("report-%Y%m%d-%H%M%S.md")), "w") as out:
        out.write(text + "\n")
    sys.exit(1 if any(r.counts()[CORRUPT] for r in results) else 0)


if __name__ == "__main__":
    main()
