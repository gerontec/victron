# Build container for Venus OS (Yocto scarthgap) - host Ubuntu 26.04 is too new for scarthgap
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    sed wget git git-lfs coreutils unzip texinfo gawk diffstat help2man make gcc build-essential g++ \
    desktop-file-utils chrpath u-boot-tools imagemagick zip python3 python3-dev python3-setuptools \
    python3-pip python3-pexpect python3-git python3-jinja2 python3-subunit xz-utils debianutils iputils-ping \
    cpio file lz4 zstd liblz4-tool locales sudo ca-certificates bzip2 patch perl rsync bc libssl-dev \
    socat libsdl2-dev xterm mesa-common-dev less nano \
 && locale-gen en_US.UTF-8 && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
# ubuntu:24.04 ships user ubuntu with uid 1000; rename it to gh so file ownership matches the host
RUN usermod -l gh -d /home/gh -m ubuntu && groupmod -n gh ubuntu
USER gh
WORKDIR /home/gh/venus
