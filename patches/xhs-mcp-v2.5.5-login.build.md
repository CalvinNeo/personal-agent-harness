# xiaohongshu-mcp v2.5.5 login patch

This patch addresses the QR login lifecycle and browser failure paths. It is
intended for the v2.5.5 source tree whose module is
`github.com/xpzouying/xiaohongshu-mcp`.

All commands keep Go caches, temporary files, and the output binary under
`/tmp`. The patch does not contain credentials or cookies.

```sh
set -eu
SRC=/path/to/xpzouying-xiaohongshu-mcp-v2.5.5
WORK=$(mktemp -d /tmp/xhs-build.XXXXXX)
cp -a "$SRC" "$WORK/src"
export GOCACHE="$WORK/gocache" GOMODCACHE="$WORK/gomodcache" GOPATH="$WORK/gopath"
export GOTMPDIR="$WORK/gotmp" TMPDIR="$WORK/tmp"
mkdir -p "$GOCACHE" "$GOMODCACHE" "$GOPATH" "$GOTMPDIR" "$TMPDIR"
cd "$WORK/src"
patch --dry-run -p1 -i /path/to/personal-agent-harness/patches/xhs-mcp-v2.5.5-login.patch
patch -p1 -i /path/to/personal-agent-harness/patches/xhs-mcp-v2.5.5-login.patch
GOPROXY=https://proxy.golang.org /opt/homebrew/bin/go mod download
GOPROXY=off /opt/homebrew/bin/go test ./...
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 GOPROXY=off \
  /opt/homebrew/bin/go build -o /tmp/xiaohongshu-mcp-login-fixed-amd64 .
```

The patch is fail-safe around page/browser creation and close operations,
uses a bounded request context for QR acquisition, uses Rod's non-`Must*`
navigation/DOM APIs, and waits for DOM readiness rather than all page assets.
A `sync.Once` protects cleanup when ownership transfers to the background scan
worker. Build and tests should be repeated against the exact upstream revision;
the patch intentionally rejects or conflicts with unrelated source versions.
