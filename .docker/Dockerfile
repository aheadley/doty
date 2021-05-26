## Dockerfile for aheadley/doty

FROM fedora:34

RUN dnf update -y \
    && dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" \
        "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm" \
    && dnf install -y @development-tools libsamplerate python-devel python-virtualenv rust cargo gcc-c++ ffmpeg youtube-dl curl unzip

COPY . /opt/doty

RUN virtualenv /opt/doty.venv \
    && /opt/doty.venv/bin/pip install -U pip \
    && /opt/doty.venv/bin/pip install -Ur /opt/doty/requirements.pre-install.txt \
    && /opt/doty.venv/bin/pip install -Ur /opt/doty/requirements.txt \
    && /opt/doty.venv/bin/pip install --no-deps -r /opt/doty/requirements.larynx.txt \
    && /opt/doty.venv/bin/snips-nlu download en

ENTRYPOINT ["/opt/doty.venv/bin/python", "/opt/doty/doty.py"]
