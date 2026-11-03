#!/bin/sh
# Daily PostgreSQL backup (run by the `backup` service of docker-compose.prod.yml, or cron).
#
#   BACKUP_DIR=/backups KEEP_DAYS=14 ./backup.sh
#
# - pg_dump in custom format (compressed, restorable table by table)
# - optional encryption at rest with a passphrase (BACKUP_PASSPHRASE, AES-256 via openssl)
# - old backups deleted after KEEP_DAYS
# Restore: see SECURITY.md ("Backups & restore").
set -eu

: "${PGHOST:=db}"
: "${PGUSER:=cod}"
: "${PGDATABASE:=cod_confirm}"
: "${BACKUP_DIR:=/backups}"
: "${KEEP_DAYS:=14}"

mkdir -p "$BACKUP_DIR"
umask 077
stamp=$(date -u +%Y%m%d-%H%M%S)
file="$BACKUP_DIR/cod_confirm-$stamp.dump"

pg_dump --format=custom --no-owner --file="$file" "$PGDATABASE"

if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$file" -out "$file.enc" \
    -pass env:BACKUP_PASSPHRASE
  rm -f "$file"
  file="$file.enc"
fi

find "$BACKUP_DIR" -name 'cod_confirm-*' -type f -mtime +"$KEEP_DAYS" -delete
echo "backup written: $file ($(du -h "$file" | cut -f1))"
