# Torture test results

Run 2026-10-02 on `opencloud-integrity:integrity-40d2a92dcb` (built with
`devtools/integrity-image/build.sh`: opencloud `integrity` with the vendored reva fork, web
fork `0cb57e6`), posix driver, async postprocessing, OpenCloud's default CSP.

| Scenario | Files | Matched | Failed visibly | Corrupt (must be 0) |
| --- | --- | --- | --- | --- |
| 1 baseline (tus + PUT, 20 concurrent, 0 B – 5 GB) | 18 | 18 | 0 | 0 |
| 2 netem loss 5% delay 200 ms (files ≤ 100 MB) | 14 | 14 | 0 | 0 |
| 3 forced overlap ×1000 (no checksum) | 1000 | 1000 | 0 | 0 |
| 4 server crash (kill -9 mid-upload and during postprocessing) | 11 | 11 | 0 | 0 |
| 5 nginx buffering + 5 s read timeout | 16 | 15 | 1 | 0 |
| 6 version restore under load (counts are downloads) | 20 | 20 | 0 | 0 |
| 7 aborted postprocessing of an older upload | 1 | 1 | 0 | 0 |
| 8 desktop client with forced timeout, drops, corruption | 4 | 3 | 1 | 0 |
| 9 web client (headless Chromium) | 9 | 7 | 2 | 0 |

Upload sessions still in processing at the end: 0.

- 3: all 1000 retries started while the first PATCH had written part of its body.
- 4: 6 sessions were stuck in processing after the crash during postprocessing; all
  recovered with `opencloud storage-users uploads sessions --resume`.
- 5: the visible failure is the 1 GB PUT, which got 504 from nginx while the server was
  still finishing it. The stored file is intact.
- 8: the corrupted upload was rejected with 460, the client cleared its resume info and
  uploaded the file again in the next sync round. It also recovered from a 5.5 min
  stalled response and two dropped connections.
- 9: the 7 corpus files (up to 5 GB) uploaded intact using the JavaScript SHA1 fallback.
  The upload with a tampered checksum and the same-fingerprint splice were both rejected
  with 460 and stored nothing.
- 0-byte files are stored correctly but have no stored checksum (`oc:checksums` empty).

## The same scenarios on unmodified upstream

`opencloud-upstream:a1982c6908` (the audited base commit, upstream reva), same harness:

| Scenario | Files | Matched | Failed visibly | Corrupt |
| --- | --- | --- | --- | --- |
| 3 forced overlap ×1000 | 1000 | 0 | 0 | **1000** |
| 6 version restore under load | 50 | 0 | 0 | **50** |
| 7 aborted postprocessing | 1 | 0 | 0 | **1** |

- 3: every upload was stored with duplicated bytes and reported as successful.
- 6: every download during the restores hashed to neither version.
- 7: the newer upload was overwritten by the old version.

## Found, not fixed (see the reva fork's FORK_NOTES.md)

- Restoring a version that has the same mtime as the current file answers 204, keeps the
  current content and makes the version unreadable (500). Pre-existing: identical on
  upstream.
- The web client's checksum needs `'wasm-unsafe-eval'` in the CSP for the fast
  WebAssembly SHA1; without it, the web fork falls back to a slower JavaScript SHA1.
