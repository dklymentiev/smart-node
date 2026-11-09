# /srv/scripts — shared utilities

Shared helpers used by node scripts. The base ships
only `lib/`; everything else is added as needed.

## lib/

- `logging.sh` — `log_info` / `log_error`, timestamped log files, and
  `latest.log` symlink rotation (`log_open <dir>`).
- `env.sh` — standard `.env` loading (`load_env <file>`), reads from
  `/srv/secrets/` by convention.

Source them from a script:

```sh
. /srv/scripts/lib/logging.sh
. /srv/scripts/lib/env.sh
```
