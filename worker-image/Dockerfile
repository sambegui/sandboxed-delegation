# Worker image: omp plus everything a coding task needs, because the sandbox has no open egress.
# Layers go from least to most volatile so a rebuild reuses the expensive ones.
# Build it rootless with Podman as the user that owns your sandbox gateway (image_pull_policy = never; see README.md). Contains no credentials.

# Pinned by the multi-arch INDEX digest (the tag resolved to this digest on 2026-10-13); the tag stays for
# readability and the digest wins. Bump both together and rebuild.
FROM docker.io/library/node:24-bookworm@sha256:3d27e5c11e5786e309ec3e03f93ae536eb36e6e5eb3714d5eb3300a36157add0

ARG OMP_VERSION=18.9.2
# SHA-256 published by the GitHub release API for the omp-linux-x64 asset (2026-10-13).
ARG OMP_SHA256=647237242329897c3a7c0f3591fdd9cafe4a4e13872baa9bbdfe45b84f9d8992

# Rust stable 1.99.0 (channel date 2026-10-01), installed from the official standalone tarballs.
# PINNED: every sha256 below is the .sha256 file published next to the tarball on static.rust-lang.org
# (cross-checked against channel-rust-stable.toml of the same date). The build refuses a tarball whose
# hash differs. Bump RUST_VERSION, RUST_DIST_DATE and all nine hashes together.
ARG RUST_VERSION=1.99.0
ARG RUST_DIST_DATE=2026-10-01
ARG RUSTC_SHA256=77171ba2a0345fdf2abc4fedda55d6de078dae7a68527c28be8c77dcc9604bd5
ARG CARGO_PKG_SHA256=d7674918d28093097614cd9728b6ca60db9ea3038f640f0bd1e9a4188c7568ce
ARG RUST_STD_GNU_SHA256=3e58dff2d0b72196b5ea4e90536e174d400de88564a52694686b81e091169933
ARG RUST_STD_MUSL_SHA256=b106b0aa4565cc3525fd3161c69203a9e39044e6e6b4d618f261c3eeda14ad50
ARG RUST_STD_WASM32_SHA256=b760b0c9b08a6843c05de0098bd6802e23178ff473c4715ae57c1d679125bf22
ARG RUST_CLIPPY_SHA256=982442e32ad8dd3f0bb3a30db74038da5f71fd9bf1a865cc71da8ea5d4ec71ec
ARG RUSTFMT_SHA256=b22c09ab9e258ec5571da170d88bd1624a4ea602e6d70d95720c5b47494cadce
ARG RUST_SRC_SHA256=3f1f9b7ed48f4596fc87889b7b3c61747336a55c9c22db1ab0c697e0aadb77aa
ARG RUST_ANALYZER_SHA256=52dfae764797c3c7f32ba2ba82d49659741a867e7a3f45bc3291b822c955d6a8

# Bun release binary, pinned by the sha256 GitHub publishes as the release asset digest.
ARG BUN_VERSION=1.4.3
ARG BUN_SHA256=3e730528d89775f03f87f05c06787c4df4099d0a37f3f252e7b829cb23816e18

# Layer 1: system tools. node:bookworm already carries git, gcc, g++, make, curl and python3.
# ripgrep and fd are installed because omp downloads them on first use and the sandbox blocks that.
# cmake/clang/lld are build backends for Rust (build scripts, -Zlinker features) and node-gyp/pip source builds;
# musl-tools makes the x86_64-unknown-linux-musl std actually linkable; python3-dev carries the headers
# pip/maturin builds compile against. The rest are the usual -dev libraries cargo and pip builds expect.
# PINNED: apt reads the Debian snapshot archive at one fixed instant (the base image itself was assembled from the
# 2026-10-05 snapshot, see its debian.sources), so a rebuild gets the same package versions however long after
# Debian has moved on. 20261013T093000Z is just before the 18.9.2-6 build; bump it deliberately and rebuild.
# Check-Valid-Until is off because a snapshot's Release files carry an expiry that has passed; the archive keyring still verifies them.
ARG DEBIAN_SNAPSHOT=20261013T093000Z
RUN printf 'Types: deb\nURIs: https://snapshot.debian.org/archive/debian/%s\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://snapshot.debian.org/archive/debian-security/%s\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "${DEBIAN_SNAPSHOT}" "${DEBIAN_SNAPSHOT}" > /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends ripgrep fd-find jq python3-venv python3-pip \
 cmake clang lld musl-tools pkg-config libssl-dev libsqlite3-dev sqlite3 zlib1g-dev libffi-dev \
 python3-dev unzip xz-utils \
 && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*

# Layer 2: Rust toolchain from the official standalone tarballs. No rustup: /opt is read-only at runtime and
# there is no egress, so the full toolchain lands under /opt/rust and only CARGO_HOME points into the writable
# /sandbox. Each tarball unpacks to one component directory (rustc/, cargo/, rust-std-<triple>/, clippy-preview/,
# rustfmt-preview/, rust-src/, rust-analyzer-preview/); all of them are merged into /opt/rust, which is the
# layout rustup would produce. rust-analyzer finds the std sources through RUST_SRC_PATH (set below).
RUN set -eu; \
 base="https://static.rust-lang.org/dist/${RUST_DIST_DATE}"; \
 work=/tmp/rust; dest=/opt/rust; mkdir -p "$work" "$dest"; \
 fetch() { \
 file="$1"; sum="$2"; \
 curl -fsSL -o "$work/$file" "$base/$file"; \
 echo "$sum  $work/$file" | sha256sum -c -; \
 mkdir -p "$work/x-$file"; \
 tar -xJf "$work/$file" -C "$work/x-$file"; \
 }; \
 fetch "rustc-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$RUSTC_SHA256"; \
 fetch "cargo-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$CARGO_PKG_SHA256"; \
 fetch "rust-std-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$RUST_STD_GNU_SHA256"; \
 fetch "rust-std-${RUST_VERSION}-x86_64-unknown-linux-musl.tar.xz" "$RUST_STD_MUSL_SHA256"; \
 fetch "rust-std-${RUST_VERSION}-wasm32-unknown-unknown.tar.xz" "$RUST_STD_WASM32_SHA256"; \
 fetch "clippy-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$RUST_CLIPPY_SHA256"; \
 fetch "rustfmt-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$RUSTFMT_SHA256"; \
 fetch "rust-src-${RUST_VERSION}.tar.xz" "$RUST_SRC_SHA256"; \
 fetch "rust-analyzer-${RUST_VERSION}-x86_64-unknown-linux-gnu.tar.xz" "$RUST_ANALYZER_SHA256"; \
 for x in "$work"/x-*/; do \
 comp=$(find "$x" -mindepth 2 -maxdepth 2 -type d | head -1); \
 cp -a "$comp"/. "$dest"/; \
 done; \
 rm -rf "$work"; \
 "$dest/bin/rustc" --version && "$dest/bin/cargo" --version && "$dest/bin/rust-analyzer" --version

# Layer 3: language tools (Python and JavaScript/TypeScript).
# PINNED by hash: requirements.lock lists every pip package (tools, build backends, pytest ecosystem and their
# dependencies) with its sha256, and pip refuses anything else; npm/package-lock.json pins every npm package by
# integrity hash and `npm ci` refuses a tree that differs from it. Regenerate both from the exact versions in
# the manifest (see README.md) and rebuild. pnpm and yarn come from those npm packages (not corepack, which is
# never enabled and must not download at runtime; our node_modules/.bin precedes /usr/local/bin in PATH).
COPY requirements.lock /opt/tools/requirements.lock
COPY npm/package.json npm/package-lock.json /opt/tools/node/
RUN python3 -m venv /opt/tools/venv \
 && /opt/tools/venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /opt/tools/requirements.lock \
 && cd /opt/tools/node && npm ci --ignore-scripts --no-audit --no-fund \
 && npm cache clean --force

# Layer 4: bun, pinned by the GitHub release asset digest.
RUN curl -fsSL -o /tmp/bun.zip \
 "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
 && echo "${BUN_SHA256}  /tmp/bun.zip" | sha256sum -c - \
 && unzip -q -o /tmp/bun.zip -d /tmp/bunx \
 && install -m 0755 /tmp/bunx/bun-linux-x64/bun /usr/local/bin/bun \
 && rm -rf /tmp/bun.zip /tmp/bunx \
 && bun --version

# Layer 5: omp, pinned and checksum-verified.
RUN curl -fsSL -o /usr/local/bin/omp \
 "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/omp-linux-x64" \
 && echo "${OMP_SHA256}  /usr/local/bin/omp" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/omp

# Runtime layout: /tmp and the working directory are the only writable paths in the sandbox policy.
# Every package manager is pointed at the shared dependency cache in the mounted workspace (/sandbox/workspace/.deps)
# and set to fail fast offline; the worker itself cannot fetch anything (see README.md "Offline convention").
# /sandbox/workspace is the mount point of the shared workspace volume. PROBED 2026-10-14: podman chowns a named volume's
# root to the owner AND mode of its mount-point directory inside the container, on every container start, and OpenShell
# starts containers as root; without this line the mount point is root-owned, so every worker (re)start would flip the host
# directory to the gateway's user and the worker (uid 1000) could no longer write. Owned by node, mode 2775 (setgid), the chown
# is a no-op for the host directory (owner and group of your workspace volume, mode 2775).
RUN mkdir -p /sandbox/workspace /opt/omp-natives && chown -R node:node /sandbox /opt/omp-natives && chmod 2775 /sandbox/workspace
USER node
ENV HOME=/tmp/home \
 PI_CODING_AGENT_DIR=/tmp/omp-agent \
 PI_NATIVES_DIR=/opt/omp-natives \
 PI_NO_TITLE=1 \
 CARGO_HOME=/sandbox/.cargo \
 CARGO_NET_OFFLINE=true \
 RUST_SRC_PATH=/opt/rust/lib/rustlib/src/rust/library \
 npm_config_offline=true \
 npm_config_audit=false \
 npm_config_fund=false \
 npm_config_cache=/sandbox/workspace/.deps/npm-cache \
 UV_OFFLINE=1 \
 PIP_DISABLE_PIP_VERSION_CHECK=1 \
 PIP_NO_INDEX=1 \
 PIP_FIND_LINKS=/sandbox/workspace/.deps/pip \
 COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
 PATH=/opt/rust/bin:/opt/tools/venv/bin:/opt/tools/node/node_modules/.bin:/usr/local/bin:/usr/bin:/bin

# Extract omp's embedded native addon into PI_NATIVES_DIR now. omp extracts it on the first real session, not on
# --version, and the sandbox has a read-only /opt and no egress, so a first-run extraction there fails (observed
# 2026-10-13: omp exited 1 with its natives recovery message). A session with no model configured still loads the
# natives first. The build is refused if nothing was extracted, and the manifest records each file's digest.
RUN HOME=/home/node timeout 90 omp -p --no-session --no-title --max-time 45 "warm natives" > /tmp/warm.out 2>&1 || true; \
 if [ -z "$(ls -A /opt/omp-natives/${OMP_VERSION} 2>/dev/null)" ]; then \
 echo "natives were not extracted into /opt/omp-natives/${OMP_VERSION}; build refused"; head -c 1500 /tmp/warm.out; exit 1; fi; \
 { echo "omp: $(omp --version)"; echo "node: $(node --version)"; echo "python: $(python3 --version)"; \
 echo "git: $(git --version)"; echo "rg: $(rg --version | head -1)"; \
 echo "rust: $(rustc --version)"; echo "cargo: $(cargo --version)"; \
 echo "rust std targets: $(ls -d /opt/rust/lib/rustlib/*/ | tr -d '/' | xargs -n1 basename | grep -v '^src$' | grep -v '^etc$' | tr '\n' ' ')"; \
 echo "rust-analyzer: $(rust-analyzer --version)"; echo "rustfmt: $(rustfmt --version)"; echo "clippy: $(cargo-clippy --version)"; \
 echo "bun: $(bun --version)"; \
 echo "--- js tools (npm ci from package-lock.json)"; \
 for t in tsc typescript-language-server pyright eslint prettier vitest tsx pnpm yarn; do printf '%s: %s\n' "$t" "$("$t" --version 2>&1 | head -1)"; done; \
 echo "node types: $(node -p "require('/opt/tools/node/node_modules/@types/node/package.json').version" 2>/dev/null)"; \
 echo "--- python"; /opt/tools/venv/bin/pip freeze; echo "uv: $(uv --version)"; \
 echo "--- pins"; echo "rust ${RUST_VERSION} (${RUST_DIST_DATE}) from static.rust-lang.org"; echo "bun v${BUN_VERSION} from github.com/oven-sh/bun releases"; \
 echo "--- lock files (sha256)"; (cd /opt/tools && sha256sum requirements.lock node/package-lock.json); \
 echo "--- apt (every installed package)"; dpkg-query -W -f='${Package} ${Version}\n' | sort; \
 echo "--- natives (sha256)"; (cd /opt/omp-natives/${OMP_VERSION} && sha256sum ./*); } > /tmp/worker-manifest.txt

USER root
RUN cp /tmp/worker-manifest.txt /opt/worker-manifest.txt && rm -f /tmp/worker-manifest.txt
USER node
WORKDIR /sandbox
LABEL org.opencontainers.image.title="omp-worker" \
 org.opencontainers.image.description="Offline coding worker: omp, Rust, TypeScript and Python toolchains" \
 worker.omp.version="${OMP_VERSION}" \
 worker.rust.version="${RUST_VERSION}" \
 worker.bun.version="${BUN_VERSION}"
