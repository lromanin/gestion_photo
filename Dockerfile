FROM python:3.12-slim

# exiftool (outil Perl) — nécessaire pour les dates EXIF de audit.py.
# En son absence, le script bascule sur mtime + signale "sans_exif".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && exiftool -ver \
    && echo "exiftool OK"

WORKDIR /app

# audit.py n'utilise que la bibliothèque standard : aucune dépendance Python runtime.
COPY audit.py /app/audit.py

# Conteneur sans daemon : reste actif pour `docker exec` ponctuel.
CMD ["sleep", "infinity"]
