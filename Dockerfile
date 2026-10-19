# Kresus (latest npm release) + Woob (GitLab master) + patched modules + Sure bridge.
#   docker compose build
#   docker compose build --build-arg WOOB_REF=3bc0f159c65f   # pin Woob to a commit
FROM node:24-bookworm

ARG KRESUS_VERSION=latest
ARG WOOB_REPO=https://gitlab.com/woob/woob.git
ARG WOOB_REF=master
ARG BETTER_SQLITE3_VERSION=12
# Woob (selenium.py) still uses find_element_by_xpath (removed in 4.3), capabilities= and service_log_path= (4.10)
ARG SELENIUM_VERSION=4.2.0
ARG GECKODRIVER_VERSION=0.36.0

ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8

# System dependencies: Python for Woob, Firefox + geckodriver for the bp module (Selenium),
# image/PDF libraries used by several modules, sqlite3 for backups.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-dev python3-venv python3-pip \
      firefox-esr \
      libxml2-dev libxslt1-dev libyaml-dev libffi-dev libjpeg-dev libopenjp2-7-dev zlib1g-dev \
      libfreetype6-dev libwebp-dev libtiff-dev mupdf-tools \
      sqlite3 tzdata curl ca-certificates git \
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
RUN git clone --depth 1 --branch "${WOOB_REF}" "${WOOB_REPO}" /opt/woob 2>/dev/null \
 || { git clone "${WOOB_REPO}" /opt/woob && git -C /opt/woob checkout "${WOOB_REF}"; }
RUN cd /opt/woob && for p in /tmp/patches/*.patch; do echo "patch $p"; git apply --verbose "$p"; done \
 && git log -1 --format='woob %h %cs' | tee /opt/woob/.build-ref

# Python environment: Woob + dependencies of the modules used by Kresus
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir /opt/woob \
 && /opt/venv/bin/pip install --no-cache-dir "selenium==${SELENIUM_VERSION}" \
      simplejson BeautifulSoup4 PyExecJS typing-extensions pdfminer.six Pillow pycountry \
      pycryptodomex pyjwt xlrd chompjs schwifty deprecated html2text \
 && /opt/venv/bin/python -c "import woob, selenium; print('woob', woob.__version__, 'selenium', selenium.__version__)"

# Kresus: latest npm release. better-sqlite3 is a development dependency of the package:
# install it separately and link it into typeorm.
RUN npm install -g "kresus@${KRESUS_VERSION}" --no-audit --no-fund \
 && mkdir -p /opt/kresus-deps \
 && npm install --prefix /opt/kresus-deps "better-sqlite3@${BETTER_SQLITE3_VERSION}" --no-audit --no-fund \
 && ln -sfn /opt/kresus-deps/node_modules/better-sqlite3 "$(npm root -g)/kresus/node_modules/better-sqlite3" \
 && cd "$(npm root -g)/kresus/node_modules/typeorm" && node -e "require('better-sqlite3'); console.log('better-sqlite3 OK')"

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
