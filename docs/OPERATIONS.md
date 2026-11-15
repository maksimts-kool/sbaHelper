# Operations

Deployment, upgrades, backups and troubleshooting for the production bot
(Portainer stack from [docker-compose.portainer.yml](../docker-compose.portainer.yml)).

## Upgrading to the hardened release (v3)

This release changes how the container runs. Do these steps **before** the new image
starts in production, in this order.

1. **Remove Watchtower.** The stack no longer contains it, but a running
   `downloader-watchtower` container keeps pulling `:latest` until it is gone. In Portainer,
   update the stack with the new compose file and enable *Prune services*, or remove the
   container by hand:

   ```bash
   docker rm -f downloader-watchtower
   ```

2. **Back up** the database and cookies while the old bot still runs (see *Backup* below
   for the consistent method; the old image has no backup command, so stop the bot first
   and copy the files):

   ```bash
   docker stop downloader-bot
   docker run --rm -v sbahelper_stats-data:/data -v "$PWD":/backup alpine \
     cp -a /data/downloader_stats.db /backup/downloader_stats-pre-v3.db
   sudo cp -a /opt/sbahelper/cookies /opt/sbahelper/cookies.pre-v3
   sudo chmod 700 /opt/sbahelper/cookies.pre-v3
   ```

   The volume name is `<stack name>_stats-data`; check it with `docker volume ls`.

3. **Non-root volume migration.** The image now runs as UID/GID **10001**. Volumes written
   by the old root container belong to root, so the bot could not write its database or
   read the cookies (it starts anyway and logs `… is not writable by UID 10001`). Give
   exactly the bot's two locations to that user:

   ```bash
   # the database volume
   docker run --rm -u 0 -v sbahelper_stats-data:/data --entrypoint sh \
     ghcr.io/maksimts-kool/sbahelper:latest -c 'chown -R 10001:10001 /data'
   # the cookie bind mount (COOKIES_HOST_DIR)
   sudo chown -R 10001:10001 /opt/sbahelper/cookies
   sudo chmod 700 /opt/sbahelper/cookies
   sudo find /opt/sbahelper/cookies -type f -exec chmod 600 {} +
   ```

   Do not `chmod 777` and do not chown anything but these paths. If you cannot migrate
   yet, `user: "0:0"` on the service runs the container as root as before; treat that as a
   temporary rollback, not a configuration.

4. **Update the stack** with the new compose file. Set new variables only if you want
   non-defaults. Notable changes:
   - `MAX_FILE_SIZE_MB` → `MAX_FILE_SIZE_BYTES` (default 45,000,000). The old variable is
     still read as N × 1,000,000 bytes and capped at 50,000,000, with a warning.
   - `ACCESS_POLICY`: unset keeps the old behaviour. A malformed `ALLOWED_CHAT_IDS` now
     admits no group instead of every chat; check the startup log for `Access=`.
5. **Watch the first start.** The log shows `Database backed up before migration`,
   `Database migrated. Version=3`, the cookie counts and `Bot started. … Access=…`. The
   container turns `healthy` within about a minute.

Rollback to the previous image: set `SBAHELPER_TAG` to its SHA and update the stack. It
works on the migrated database as is (it only uses the `downloads` table, which the
migrations leave unchanged, and it runs as root, so the new ownership does not stop it);
the `.pre-v3-*.bak` copy is there if you want the exact old file back.

## Updates and rollback

Nothing updates the bot automatically. CI publishes two tags after every green push to
`main`: `:latest` and the immutable `:<full commit SHA>`.

- **Update:** in the stack, set `SBAHELPER_TAG` (or leave `latest`) and choose *Update the
  stack* with *Re-pull image and redeploy*.
- **Pin:** set `SBAHELPER_TAG` to a SHA from the GHCR package page. That exact image runs
  until you change the variable.
- **Rollback:** set `SBAHELPER_TAG` to the previous SHA and update the stack. This bot
  refuses a database whose schema is newer than it knows (`Database refused: … newer`)
  instead of writing to it; in that case restore the `*.pre-v<N>-*.bak` copy the newer
  version wrote before migrating (see *Restore*).

**One instance per token.** Two containers polling the same token fight over updates
(Telegram returns *Conflict*; the bot alerts admins). Keep the service and container name
`downloader-bot`: Docker refuses a second container with the same name, and renaming the
service in Portainer would leave the old container running next to the new one.

## Backup

The database is SQLite in WAL mode. Copying only `downloader_stats.db` while the bot writes
can lose recent data; use the online backup, which is consistent while the bot runs:

```bash
docker exec downloader-bot python -m sbahelper.backup create
# → /data/backups/downloader_stats-<UTC time>.db  (0600, integrity-checked)
docker exec downloader-bot python -m sbahelper.backup verify /data/backups/<file>.db
docker cp downloader-bot:/data/backups/<file>.db ./
```

The bot also writes `downloader_stats.db.pre-v<N>-<time>.bak` before every schema
migration. Backups contain chat and user ids: store them like personal data and delete old
ones. Cookie files are credentials: back them up only to a place with the same protection
(0700 directory, 0600 files, owned by the account that runs the bot).

## Restore

1. `docker stop downloader-bot` (a restore while the bot runs would be overwritten).
2. Verify the copy: `docker run --rm -v "$PWD":/b ghcr.io/maksimts-kool/sbahelper:<tag> python -m sbahelper.backup verify /b/<file>.db`
3. Put it in place as the bot's user, removing stale WAL files:

   ```bash
   docker run --rm -u 10001:10001 -v sbahelper_stats-data:/data -v "$PWD":/b --entrypoint sh \
     ghcr.io/maksimts-kool/sbahelper:<tag> -c \
     'rm -f /data/downloader_stats.db-wal /data/downloader_stats.db-shm &&
      cp /b/<file>.db /data/downloader_stats.db && chmod 600 /data/downloader_stats.db'
   ```

   Files copied in as root must be chowned to 10001:10001 afterwards.
4. Start the bot; a restored older schema is migrated (and backed up) again on start.

## Health and limits

- The bot rewrites `/tmp/sbahelper.heartbeat` every 30 s; the image's `HEALTHCHECK` runs
  `python -m sbahelper.health`, which only reads that file (no Telegram call, no download).
  A hung event loop stops the heartbeat and the container becomes **unhealthy**. Docker
  does not restart unhealthy containers by itself: `restart: unless-stopped` reacts to the
  process exiting. Use an autoheal tool or your monitoring if you want restarts on
  unhealthy.
- `python -m sbahelper.health --deps` checks ffmpeg, ffprobe, deno and the yt-dlp private
  APIs the bot relies on (CI runs it in the image).
- Container hardening: read-only root filesystem, `/tmp` tmpfs (`SBAHELPER_TMP_SIZE`,
  default 768m; it counts towards the memory limit), all capabilities dropped,
  `no-new-privileges`, `mem_limit` 1536m, 2 CPUs, 256 processes, JSON logs rotated at
  3 × 10 MB, SIGTERM with a 40 s grace period (the bot drops queued jobs with a "restarting"
  notice and gives running ones 20 s).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `… is not writable by UID 10001` | Volume not migrated: see step 3 above. |
| Bot ignores links in a group, `/download` works | Privacy Mode is on and the bot is not an admin, or auto-download is off in `/settings`. |
| `Another bot instance is polling with the same token` | A second container or a developer machine uses the production token. |
| `Database refused: … newer than this bot` | Rolled back the image without restoring the pre-migration backup. |
| `Cookies needed: the site asks to log in` | Send fresh cookies (README → Cookies). |
| `No usable impersonation target` / `Blocked` | TikTok blocks the curl-cffi fingerprints: update yt-dlp/curl-cffi or `IMPERSONATE_TARGETS`. |
| `Video tools missing` | The image lost ffmpeg/ffprobe; rebuild. |
| Users see "Сейчас слишком много загрузок" | Queue full: raise `MAX_ACTIVE_DOWNLOADS` / `MAX_QUEUED_DOWNLOADS` if the host can take it. |
| Group became a supergroup | History and settings move automatically; add the new id to `ALLOWED_CHAT_IDS` (the bot logs a warning with both ids). |
