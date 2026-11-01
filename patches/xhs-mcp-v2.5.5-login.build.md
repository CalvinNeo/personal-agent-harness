# xiaohongshu-mcp v2.5.5 cumulative login patch

This patch addresses the QR login lifecycle, persisted state and browser failure paths. It is
intended for the v2.5.5 source tree whose module is
`github.com/xpzouying/xiaohongshu-mcp`.

The nine changed/new files are pinned in `xhs-mcp-v2.5.5-manifest.json`.
Check the original hashes before applying and patched hashes afterwards; a null
original hash requires an absent file. Do not stack this cumulative patch on an
older login patch. Restore the exact upstream baseline first.

Commands below use the Go version requested by the upstream `go.mod`; `go` must
be on PATH. Caches, temporary files and output binaries stay outside the repo.
The patch does not contain credentials or cookies.

```sh
set -eu
SRC=/path/to/xpzouying-xiaohongshu-mcp-v2.5.5
REPO=/path/to/personal-agent-harness
WORK=$(mktemp -d /tmp/xhs-build.XXXXXX)
cp -a "$SRC" "$WORK/src"
export GOCACHE="$WORK/gocache" GOMODCACHE="$WORK/gomodcache" GOPATH="$WORK/gopath"
export GOTMPDIR="$WORK/gotmp" TMPDIR="$WORK/tmp"
mkdir -p "$GOCACHE" "$GOMODCACHE" "$GOPATH" "$GOTMPDIR" "$TMPDIR"
cd "$WORK/src"
patch --dry-run --batch -p1 -i "$REPO/patches/xhs-mcp-v2.5.5-login.patch"
patch --batch -p1 -i "$REPO/patches/xhs-mcp-v2.5.5-login.patch"
GOPROXY=https://proxy.golang.org go mod download
GOPROXY=off go test ./...
GOPROXY=off go test -race ./...
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 GOPROXY=off \
  go build -o "$WORK/xiaohongshu-mcp-amd64" .
mkdir -p cmd/status-diagnostic
cp "$REPO/patches/xhs-tools/status-diagnostic/main.go" cmd/status-diagnostic/main.go
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 GOPROXY=off \
  go build -o "$WORK/xhs-status-diagnostic-amd64" ./cmd/status-diagnostic
```

The patch is fail-safe around page/browser creation and close operations,
uses a bounded request context for QR acquisition, uses Rod's non-`Must*`
navigation/DOM APIs, and polls the login markers without waiting for the
page's full `readyState` (which can hang behind anti-bot/analytics requests).
A `sync.Once` protects cleanup when ownership transfers to the background scan
worker. QR resources remain alive while scanning; status distinguishes a live
pending session from persisted authentication. Cookies are atomically replaced
with private permissions only after successful login, not merely after a scan.
The optional status diagnostic reports booleans rather than page HTML, images,
cookies or account IDs; do not run it as part of unattended build tests.

Build and tests should be repeated against the exact upstream revision;
the patch intentionally rejects or conflicts with unrelated source versions.
