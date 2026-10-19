# Kresus + Woob (GitLab) + patched modules + Sure bridge.
# Multi-stage build: everything needed to compile (compilers, -dev packages, git, npm cache) stays in the
# "builder" stage; the final image only gets the installed result.
#
# Kresus, Woob, Selenium and geckodriver are pinned so that the patches in patches/ keep applying (they are made
# for one precise Woob commit). The base image is not pinned: it follows the node:24-bookworm-slim tag.
# To upgrade, change the values below (or pass --build-arg), regenerate the patches with
# scripts/make-patches.sh, then tag a new release:
#   docker compose build --build-arg KRESUS_VERSION=latest --build-arg WOOB_REF=master
ARG NODE_IMAGE=node:24-bookworm-slim

# ---------------------------------------------------------------------------- builder
FROM ${NODE_IMAGE} AS builder

ARG KRESUS_VERSION=0.26.0
ARG WOOB_REPO=https://gitlab.com/woob/woob.git
# Branch, tag or full commit SHA (this one is the master the patches were made against)
ARG WOOB_REF=3bc0f159c65fc2685c8b8787d0e25ff55f896f73
ARG BETTER_SQLITE3_VERSION=12
# Woob (selenium.py) still uses find_element_by_xpath (removed in 4.3), capabilities= and service_log_path= (4.10)
ARG SELENIUM_VERSION=4.2.0
ARG GECKODRIVER_VERSION=0.36.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-dev python3-venv build-essential git curl ca-certificates \
      libxml2-dev libxslt1-dev libyaml-dev libffi-dev libjpeg-dev libopenjp2-7-dev zlib1g-dev \
      libfreetype6-dev libwebp-dev libtiff-dev \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) gk=linux64 ;; arm64) gk=linux-aarch64 ;; *) echo "unsupported architecture: $arch"; exit 1 ;; esac; \
    curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gk}.tar.gz" \
      | tar -xz -C /usr/local/bin geckodriver; \
    geckodriver --version | head -1

# Woob: master, then our patches (boursedirect, esalia, fortuneo). The build fails if the patches
# no longer apply on this master: regenerate them with scripts/make-patches.sh.
COPY patches/ /tmp/patches/
RUN git init -q /opt/woob && cd /opt/woob \
 && git remote add origin "${WOOB_REPO}" \
 && git fetch -q --depth 1 origin "${WOOB_REF}" \
 && git checkout -q FETCH_HEAD
RUN cd /opt/woob && for p in /tmp/patches/*.patch; do echo "patch $p"; git apply --verbose "$p"; done \
 && git log -1 --format='woob %h %cs' > /tmp/build-ref

# Python environment: Woob + dependencies of the modules used by Kresus
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir /opt/woob \
 && /opt/venv/bin/pip install --no-cache-dir "selenium==${SELENIUM_VERSION}" \
      simplejson BeautifulSoup4 PyExecJS typing-extensions pdfminer.six Pillow pycountry \
      pycryptodomex pyjwt xlrd chompjs schwifty deprecated html2text \
 && /opt/venv/bin/python -c "import woob, selenium; print('woob', woob.__version__, 'selenium', selenium.__version__)" \
 && /opt/venv/bin/pip uninstall -y pip wheel \
 && find /opt/venv -name __pycache__ -type d -prune -exec rm -rf {} +

# Kresus: latest npm release. better-sqlite3 is a development dependency of the package:
# install it separately and link it into typeorm.
RUN npm install -g "kresus@${KRESUS_VERSION}" --no-audit --no-fund \
 && mkdir -p /opt/kresus-deps \
 && npm install --prefix /opt/kresus-deps "better-sqlite3@${BETTER_SQLITE3_VERSION}" --no-audit --no-fund \
 && ln -sfn /opt/kresus-deps/node_modules/better-sqlite3 "$(npm root -g)/kresus/node_modules/better-sqlite3" \
 && cd "$(npm root -g)/kresus/node_modules/typeorm" && node -e "require('better-sqlite3'); console.log('better-sqlite3 OK')" \
 && npm cache clean --force \
 && rm -rf /opt/kresus-deps/node_modules/better-sqlite3/deps /opt/kresus-deps/node_modules/better-sqlite3/src

# Only what the runtime needs from Woob: the modules (the woob package itself is in the venv)
RUN mkdir /out && cp -r /opt/woob/modules /out/modules && cp /tmp/build-ref /out/.build-ref \
 && find /out -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------- runtime
FROM ${NODE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8

# Runtime only: Python (the venv is copied from the builder, same Debian release), Firefox for the bp
# module (Selenium), curl for the healthcheck, tzdata for TZ. No compilers, no git, no -dev packages.
# npm, yarn and corepack come with the node image but Kresus does not need them at runtime; the icon theme,
# docs and man pages pulled in by Firefox's GTK dependencies are useless in a headless container.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 firefox-esr tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn-* \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg \
      /usr/local/include /usr/share/icons /usr/share/doc /usr/share/man /usr/share/info /usr/share/lintian

COPY --from=builder /usr/local/bin/geckodriver /usr/local/bin/geckodriver
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/kresus-deps /opt/kresus-deps
COPY --from=builder /usr/local/lib/node_modules/kresus /usr/local/lib/node_modules/kresus
COPY --from=builder /out /opt/woob
RUN ln -s ../lib/node_modules/kresus/bin/kresus.js /usr/local/bin/kresus

# Sure bridge + entrypoint (CRLF stripped in case the files were checked out on Windows)
COPY sure-bridge/sure-bridge.py /usr/local/bin/sure-bridge.py
COPY scripts/entrypoint.sh scripts/sure-bridge-scheduler.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/sure-bridge.py /usr/local/bin/*.sh \
 && chmod 755 /usr/local/bin/sure-bridge.py /usr/local/bin/entrypoint.sh /usr/local/bin/sure-bridge-scheduler.sh \
 && useradd -r -m -d /var/lib/kresus -s /bin/bash -u 9001 kresus \
 && mkdir -p /var/lib/sure-bridge \
 && chown -R kresus:kresus /var/lib/kresus /var/lib/sure-bridge /opt/woob

ENV HOME=/var/lib/kresus \
    NODE_ENV=production \
    NODE_PATH=/opt/kresus-deps/node_modules \
    PATH=/opt/venv/bin:$PATH \
    KRESUS_PYTHON_EXEC=/opt/venv/bin/python \
    KRESUS_DIR=/var/lib/kresus \
    KRESUS_WOOB_SOURCES_LIST=/var/lib/kresus/sources.list \
    HOST=0.0.0.0 \
    PORT=9876 \
    TZ=Europe/Paris

# /var/lib/kresus: SQLite database, encryption salt, sessions, positions exported by the modules.
# /var/lib/sure-bridge: bridge state (what has already been sent to Sure).
VOLUME ["/var/lib/kresus", "/var/lib/sure-bridge"]
EXPOSE 9876
USER kresus
WORKDIR /var/lib/kresus
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["kresus"]
