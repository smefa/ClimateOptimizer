## Remote: Home Assistant (10.10.10.30, HA OS 18.2, SSH port 22, user root)
- Command template (PowerShell — note `$env:USERPROFILE`, NOT `%USERPROFILE%`):
  `ssh -i "$env:USERPROFILE\.ssh\F56hgfnTRRe5yhddbSSfdffh" -p 22 -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=10 root@10.10.10.30 "<command>"`
- **PowerShell/SSH quirks (verified on this machine):**
  - PowerShell does NOT expand `%USERPROFILE%` — always use `$env:USERPROFILE`, or the key path will not be found.
  - Always pass `-o BatchMode=yes -o ConnectTimeout=10` so a failed auth fails fast instead of hanging at a `password:` prompt (a hung prompt queues input and garbles the terminal).
  - Never attempt interactive password auth or store the HA root password anywhere; if key auth fails, stop and report to the user.
  - Generating a key: pass the empty passphrase as `-N '""'` — PowerShell 5.1 drops a bare `-N ""` and `ssh-keygen` errors with "Too many arguments".
- **Automatic (read-only):** `ha core info`, `ha core logs`, `ha core check`, `ha supervisor logs`, `ha os info`, `ha addons list`, and reading `/config/*` files.
- **Require approval first:** `ha core restart`, stopping/rebooting, editing anything under `/config`, deleting anything, and any service call that changes state.
- HA config lives at `/config` (e.g. `/config/configuration.yaml`). After an approved config edit, run `ha core check` first and only restart if it passes.
- The OS root filesystem is read-only — do NOT attempt to modify Supervisor or OS files.