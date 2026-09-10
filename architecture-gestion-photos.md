# Architecture — Gestion de photos personnelles

## 1. Contexte

- **Stockage primaire** : NAS TrueNAS Scale (~400 Go), organisation `AAAA/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext`
- **Backup** : Mega, actuellement synchronisé manuellement
- **Ingestion actuelle** : copie manuelle depuis carte SD/câble, puis scripts de renommage séparés (photos / vidéos)
- **Problème** : dossiers temporaires accumulés au fil du temps, doublons et manques non identifiés

## 2. Où tourne le script

**Dans un conteneur Docker**, déployé via `deploy.sh` (même modèle que les autres projets, ex. `telegram-anki-bot`) : tests locaux → commit/push git → SSH vers le NAS → `git pull` + `docker compose up --build -d`.

Le conteneur ne fait tourner aucun service en continu : il reste actif via `sleep infinity` pour permettre des commandes ponctuelles via `docker exec`, à la demande (pas de démon, pas d'API).

```bash
ssh rludovic@192.168.1.31 "docker exec photo-manager python3 audit.py --scan /data/photos_et_videos /data/a_trier --output /data/rapports/rapport_audit.csv"
```

## 3. Arborescence sur le NAS

```
/mnt/maisonprincipal/
├── Medias/
│   └── photos_new/
│       ├── photos_et_videos/          ← collection finale, organisée
│       │   └── AAAA/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
│       ├── _a_trier/                  ← fichiers réels, dépôt après copie SD/câble
│       ├── _doublons_detectes/        ← fichiers réels, en quarantaine
│       └── _sans_date_exif/           ← fichiers réels, sans date fiable trouvée
│
├── Projets/
│   └── gestion_photo/                 ← code (dépôt git)
│       ├── Dockerfile
│       ├── docker-compose.yml
│       ├── deploy.sh
│       ├── audit.py
│       ├── ingest.py
│       ├── requirements.txt
│       └── tests/
│
└── Projets_donnees/
    └── gestion_photo/
        └── rapports/                  ← CSV uniquement (jamais de médias ici)
            ├── rapport_audit.csv
            └── doublons_log.csv
```

**Important — nature du contenu de chaque dossier :**

- `_a_trier/`, `_doublons_detectes/`, `_sans_date_exif/` contiennent des **fichiers photo/vidéo réels**, jamais de CSV
- Les rapports (CSV) vivent exclusivement dans `Projets_donnees/gestion_photo/rapports/`, pour ne jamais mélanger métadonnées et médias
- Le conteneur Docker monte les 4 dossiers de `Medias/photos_new/*` ainsi que `rapports/` en volumes (voir `docker-compose.yml`)

## 4. Comment on interagit avec le script

Tout se pilote **en ligne de commande via SSH**, pas d'interface web dans cette première version. Chaque script a un rôle unique et prévisible.

### `audit.py` — état des lieux (lecture seule, ne modifie rien)

```bash
docker exec photo-manager python3 audit.py --scan /data/photos_et_videos /data/a_trier --output /data/rapports/rapport_audit.csv
```

Produit un CSV avec, pour chaque fichier : chemin, date EXIF trouvée (ou absente), hash, statut (`ok`, `doublon`, `sans_exif`, `mal_nomme`).

### `ingest.py` — traitement des nouveaux fichiers

```bash
docker exec photo-manager python3 ingest.py --source /data/a_trier --dest /data/photos_et_videos
```

- Renomme et range selon le format `AAAA/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext`
- Détecte les doublons via `hashes.db` → les déplace dans `_doublons_detectes/` au lieu de les intégrer
- Les fichiers sans date EXIF exploitable vont dans `_sans_date_exif/` pour traitement manuel
- Rien n'est jamais supprimé automatiquement ; le script écrit un log détaillé de chaque action

### Validation des doublons — fonctionnement détaillé

**Ce qui est comparé** : un hash SHA-256 (empreinte du contenu binaire). Deux fichiers avec le même hash sont identiques bit à bit — pas "probablement similaires", réellement identiques. Ce système ne détecte donc que les copies strictes (pas les photos recadrées/recompressées, qui relèvent d'un mécanisme différent, hors périmètre initial — voir §7).

**Ce qui est déplacé en quarantaine** : uniquement le nouveau fichier (venant de `_a_trier`), jamais l'original déjà présent dans `photos_et_videos/`. L'original n'est jamais touché automatiquement.

À côté du fichier en quarantaine (dans `Medias/_doublons_detectes/`), un log CSV **séparé** (dans `rapports/doublons_log.csv`) liste pour chaque doublon :
```
fichier_en_quarantaine, fichier_original_correspondant, hash
_doublons_detectes/20240612-143022.jpg, photos_et_videos/2024/20240612/20240612-143022.jpg, a3f5e8...
```

**Deux issues possibles pour un fichier en quarantaine :**

- **Confirmé comme doublon** → suppression du fichier en quarantaine, l'original reste en place, terminé
- **Décision de garder les deux** (cas rare, puisque le fichier est rigoureusement identique à l'original) → réintégration dans `photos_et_videos/` via `resolve_doublons.py`. Comme le nom cible calculé est identique à celui de l'original (mêmes métadonnées EXIF), un suffixe `_dup` est ajouté pour éviter d'écraser l'original : `20240612-143022_dup.jpg`

**Processus de validation, en deux temps :**

1. **Phase de rodage** : vérification manuelle des premiers passages du script (comparaison visuelle si besoin) pour t'assurer de la fiabilité de la détection, puis suppression manuelle des doublons confirmés
2. **Une fois confiant dans le script** : purge automatique différée, par exemple via une tâche cron qui supprime tout fichier présent en quarantaine depuis plus de 7 jours (fenêtre de sécurité en cas de découverte tardive d'un problème) :

```bash
# purge_doublons.sh — supprime les fichiers en quarantaine depuis plus de 7 jours
find /mnt/maisonprincipal/Medias/_doublons_detectes -type f -mtime +7 -delete
```

### `sync_mega.sh` — sauvegarde automatisée

Exécuté par une tâche cron (quotidienne ou hebdomadaire, à définir), appelle `rclone sync` vers un remote Mega configuré une fois pour toutes. Log de chaque exécution dans `rapports/sync_mega_AAAAMMJJ.log`.

```cron
0 3 * * * docker exec photo-manager /app/sync_mega.sh
```

## 5. Flux global

```
Carte SD / câble
      │  (copie manuelle, inchangé)
      ▼
Medias/_a_trier/
      │  ingest.py (via docker exec, manuel ou cron)
      ▼
   ┌──┴───────────────────┐
   ▼                       ▼
Medias/photos_et_videos/    Medias/_doublons_detectes/  →  validation manuelle → suppression
(rangé, renommé)      Medias/_sans_date_exif/     →  traitement manuel

Medias/photos_et_videos/  ──(cron rclone sync)──►  Mega
```

## 6. Phasage

1. **Audit** — lancer `audit.py` sur l'existant complet (`photos_et_videos/` + tous les dossiers temporaires), obtenir une vue claire des doublons et manques
2. **Nettoyage** — traiter le rapport, vider progressivement les dossiers temporaires
3. **Ingestion automatisée** — `ingest.py` remplace les deux scripts actuels (photo + vidéo unifiés)
4. **Sync Mega automatisée** — cron + rclone remplace l'upload manuel

## 7. Évolutions possibles (hors périmètre initial)

- Interface web de visualisation type Immich/PhotoPrism, en plus (pas à la place) de cette base de scripts, une fois la collection assainie
- Détection de quasi-doublons (photo recadrée/recompressée) via perceptual hashing, si le hash exact ne suffit pas
- Notification (email/Slack) en fin de traitement `ingest.py` ou `sync_mega.sh`
