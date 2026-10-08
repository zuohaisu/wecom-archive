# Backup and Recovery Reference

> Generalized operational reference for this project's backup chain. It
> describes the design and the verified command patterns; host-specific
> state (real hosts, accounts, live thresholds) lives in your own
> operations records, not in this repository.

## Design summary

- **Database**: nightly `pg_dump` (custom format) per tenant-capable
  database, encrypted with GPG symmetric encryption before it leaves the
  backup directory.
- **Media**: nightly `tar czf` of the media store, encrypted the same way.
- **Passphrases**: a single `BACKUP_GPG_PASSPHRASE` value, stored only in
  a root-readable env file on the host and in the operator's password
  manager. It must never appear in argv, logs, or tickets.
- **Retention**: encrypted artifacts rotate on a fixed schedule; the GPG
  home (`GNUPGHOME`) contains only session/trust state and is trivially
  recreatable (`mkdir -p && chmod 700`), so it is deliberately **not**
  backed up.
- **Disaster-recovery config bundle**: the files needed to stand up a
  *new* host (runtime `.env`, tenant private-key material, certificates)
  are packaged by `scripts/dr_config_bundle.sh` into an encrypted bundle
  stored off-host. The inventory is an explicit allowlist in that script —
  review it before changing what ships in the bundle.

## Passphrase handling rules

1. **Never in argv.** A passphrase passed as a command-line argument is
   visible to every local user via `ps`/`/proc`, and survives in shell
   history if typed interactively. Always feed it via stdin
   (`--passphrase-fd 0`).
2. **Never expanded by an outer shell.** Wrapping a command in double
   quotes (e.g. `sudo bash -c "... '$PASSPHRASE' ..."`) expands the
   passphrase into the child process's argv — this defeats rule 1 even
   though gpg itself reads stdin. The *entire script* must be passed in
   single quotes so the expansion happens inside the child shell.
3. **Fail closed.** If the passphrase file is missing or unreadable, the
   backup must fail — never fall back to writing unencrypted artifacts.

### Verified restore pattern (decryption)

```bash
# The whole script is single-quoted: nothing expands in the calling shell,
# and the passphrase only exists inside the child shell + stdin pipe.
sudo -u <deploy-user> bash -c '
  set -e
  GNUPGHOME=/srv/apps/wecom-archive-365/shared/gnupg
  PASSPHRASE=$(grep "^BACKUP_GPG_PASSPHRASE=" /srv/apps/wecom-archive-365/shared/backup.env | cut -d= -f2-)
  printf "%s" "$PASSPHRASE" | \
    gpg --batch --yes --passphrase-fd 0 --pinentry-mode loopback -d "<backup-file>.dump.gpg" > /srv/apps/wecom-archive-365/shared/backups/.tmp/restored.dump
'
```

Automation (`scripts/backup_once.sh`) uses the same rule set: the
passphrase is read from the protected env file and piped via
`--passphrase-fd 0`; the script refuses to run if the env file is absent,
so there is never a "write plaintext now, encrypt later" intermediate
state.

## Restore procedure (database)

```bash
# 1. Decrypt — see the pattern above. NEVER restore into the production database.
sudo -u <deploy-user> bash -c '<single-quoted decrypt script>'

# 2. Restore into a scratch database
sudo -u postgres createdb <scratch-db>
sudo -u <deploy-user> cat .../backups/.tmp/restored.dump | \
  sudo -u postgres pg_restore -d <scratch-db> --no-owner --no-privileges

# 3. Verify (row counts + content checksums against the manifest)

# 4. Clean up
sudo -u postgres dropdb <scratch-db>
rm -f .../backups/.tmp/restored.dump
```

Media archives restore the same way with `tar tz` / `tar xz` in place of
`pg_restore`.

## DR config bundle

`scripts/dr_config_bundle.sh` packages recovery-critical *configuration*
(runtime `.env`, on-disk tenant key material, certs) — not database or
media data — into one GPG-encrypted archive with a checksum manifest, for
storage at two off-host destinations. Its passphrase
(`RECOVERY_PASSPHRASE`) is deliberately independent of
`BACKUP_GPG_PASSPHRASE` and is likewise passed only via stdin.

Operating notes:

- Both passphrases live in the operator's password manager; scripts never
  read the recovery passphrase from disk.
- Store bundles **encrypted only** — destinations must never receive a
  plaintext copy.
- Re-verify a restore from the bundle periodically; a backup that has
  never been restored is a hypothesis, not a backup.

## What is deliberately out of scope here

- Host inventory, live thresholds, and per-host quirks — keep those in
  your own (non-published) operations records.
- The deploy user's sudo capability model — see
  [deploy-sudoers.md](deploy-sudoers.md).
- TLS renewal — see [wildcard-ssl-renewal.md](wildcard-ssl-renewal.md).
