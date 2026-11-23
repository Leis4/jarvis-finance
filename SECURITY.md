# Security and privacy

JARVIS Finance is built for one owner and one local machine.

## Data boundary

- The server binds to `127.0.0.1` and validates the `Host`/`Origin` headers.
- The browser never receives provider API keys. AI calls, when enabled, happen
  server-side behind a budget journal.
- Bank access is optional, read-only and requires the owner to authenticate in
  person. The PIN is not persisted by the app.
- Imports are staged and validated before they reach the ledger.
- Backups are local and verified; they are not uploaded anywhere.

## What is not in this repository

- `.env` files and real API keys
- owner keys, cookies or session files
- SQLite databases, statements, SMS exports or transaction history
- logs, backups or model checkpoints

Screenshots use synthetic demo data.

## Reporting

If you find a security issue, please open a private security advisory on GitHub
or contact the repository owner through the GitHub profile. Do not publish
exploitable details in a public issue.
