#!/usr/bin/env bash
# One-time setup on an Amazon Linux 2023 EC2 instance (arm64 or x86_64).
# Checks the IVS Linux broadcast SDK's runtime requirements, installs Go 1.24
# and a C compiler for cgo, then builds the bridge, the end-to-end probe, and
# nova-check. `go build` downloads the SDK's Go module, which includes the
# native library (libaws-ivs-realtime.so) for x86_64 and arm64.
#
# Run from examples/nova-sonic-webrtc/:  ./infrastructure/setup-al2023.sh
set -euo pipefail

GO_VERSION=1.24.5
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# glibc must be >= 2.34 for libaws-ivs-realtime.so (AL2023 ships 2.34).
# getconf exits cleanly, unlike `ldd --version | head`, which can trip pipefail.
getconf GNU_LIBC_VERSION

# The SDK also needs libcurl 7.68 or later (libcurl.so.4) and a CA certificate
# bundle. AL2023 preinstalls both: libcurl-minimal and ca-certificates.
# Installing the full libcurl package conflicts with libcurl-minimal.
# grep without -q reads all of ldconfig's output, so pipefail never sees SIGPIPE.
if ! /sbin/ldconfig -p | grep 'libcurl\.so\.4 ' >/dev/null; then
  echo "libcurl.so.4 not found; install libcurl-minimal (dnf install -y libcurl-minimal)" >&2
  exit 1
fi
if [ ! -s /etc/pki/tls/certs/ca-bundle.crt ]; then
  echo "CA certificate bundle not found; install ca-certificates (dnf install -y ca-certificates)" >&2
  exit 1
fi

sudo dnf install -y gcc

case "$(uname -m)" in
  aarch64) GOARCH=arm64 ;;
  x86_64)  GOARCH=amd64 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go${GO_VERSION}"; then
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz" -o /tmp/go.tgz
  sudo rm -rf /usr/local/go
  sudo tar -C /usr/local -xzf /tmp/go.tgz
  rm /tmp/go.tgz
fi
export PATH=/usr/local/go/bin:$PATH
# The SDK's Go package calls the native library through cgo; it does not build
# with CGO_ENABLED=0.
export CGO_ENABLED=1

(cd "$ROOT/src/bridge" && go build -o bin/bridge . && go build -o bin/e2e-probe ./cmd/e2e-probe && go build -o bin/nova-check ./cmd/nova-check)

echo "Setup complete. Next: create the bot token, then BOT_TOKEN_FILE=<file> ./infrastructure/run.sh (see README)"
