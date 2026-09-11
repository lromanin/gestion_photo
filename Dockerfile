FROM python:3.12-slim

# exiftool est nécessaire pour une extraction fiable des dates EXIF/QuickTime
RUN apt-get update && \
    apt-get install -y --no-install-recommends libimage-exiftool-perl && \
    rm -rf /var/lib/apt/lists/*

# Utilisateur non-root, UID/GID alignés sur l'utilisateur "photomgr" créé
# sur TrueNAS (Credentials > Local Users). C'est cet UID numérique qui
# détermine les droits réels sur les fichiers montés en volume — le nom
# n'a pas d'importance en soi, seul le nombre compte.
ARG APP_UID=1500
ARG APP_GID=1500
RUN groupadd --gid ${APP_GID} photomgr && \
    useradd --uid ${APP_UID} --gid ${APP_GID} --no-create-home --shell /usr/sbin/nologin photomgr

WORKDIR /app

COPY --chown=photomgr:photomgr . .

USER photomgr

# Le conteneur ne fait rien tourner en continu : on l'utilise via
# `docker exec` pour lancer audit.py / ingest.py à la demande.
CMD ["sleep", "infinity"]
