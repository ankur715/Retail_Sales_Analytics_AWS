FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm
# The image's Yarn apt source has an expired signing key (NO_PUBKEY 62D54FD4003F6525), which makes
# every feature's `apt-get update` fail (docker-in-docker first). Nothing here needs Yarn.
RUN rm -f /etc/apt/sources.list.d/yarn.list
