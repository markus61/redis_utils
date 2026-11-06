# redis_utils

Small command-line tools for working with Redis servers.

## redis-copy-urls

Copies every key from one Redis database to another, given as two Redis URLs.

```bash
redis-copy-urls <source_url> <target_url>
redis-copy-urls redis://83.137.169.244:6379/12 redis://localhost/12
```

- Copies all key types (string, list, hash, set, sorted set, stream, …) together with their remaining expiry time. Keys without an expiry stay permanent.
- **Keys that already exist in the target are overwritten.** Other keys in the target are left alone.
- A URL without a database number (`redis://localhost`) means database 0.
- Passwords and TLS go in the URL as usual: `redis://:password@host:6379/0`, `rediss://host:6380/0`.
- `redis-copy-urls --help` shows the requirements and exit codes.

### Requirements

| What | Requirement |
|---|---|
| Python | 3.10 or newer, with the `venv` module (Debian/Ubuntu: `apt install python3-venv`) |
| Redis servers | 3.0 or newer on both sides |
| Version direction | The target must run the **same or a newer** Redis version than the source |

The version direction matters because keys are copied with Redis's `DUMP`/`RESTORE` commands. `DUMP` writes each key in a format tied to the source server's version. A newer server can read an older server's format, but an older one can't read a newer one's.

- Redis 5 → Redis 8 works.
- Redis 8 → Redis 5 fails. The script notices this at the first key, explains the problem, and exits with code 3 without copying anything.
- If the target is older than the source, the script logs a warning before it starts. It still tries, because versions close together (for example 7.0 and 7.2) often share the same format.

Some managed Redis services block `DUMP` or `RESTORE`; the script can't be used with those.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All keys copied |
| 1 | Some keys failed; each failure is in the log |
| 2 | Could not connect to the source or the target |
| 3 | The target Redis is too old to read the source's key format; nothing was copied |

Log messages go to stderr.

### Deployment

The repository is meant to be cloned as-is onto each machine. Nothing needs to be installed by hand except Python.

```bash
git clone <repo-url> /opt/redis_utils
ln -s /opt/redis_utils/bin/redis-copy-urls /usr/local/bin/redis-copy-urls
redis-copy-urls --help
```

On its first run, the wrapper creates a virtualenv in `/opt/redis_utils/venv` and installs the libraries from `requirements.txt`. This needs internet access (or a PyPI mirror) once. After that it runs straight away.

To update, run `git pull` in the clone. If `requirements.txt` changed, the wrapper reinstalls the libraries on its next run.

To use a specific Python for the virtualenv, set `PYTHON` on the first run:

```bash
PYTHON=/usr/bin/python3.12 redis-copy-urls --help
```

To rebuild the virtualenv from scratch, delete `venv/` and run the command again.

The user running the command must be able to write to the clone directory on the first run and after updates, because that is where `venv/` lives.

### Files

| File | Purpose |
|---|---|
| `redis_copy_urls.py` | The copy script |
| `bin/redis-copy-urls` | Bash wrapper: finds the clone through symlinks, creates or updates `venv/`, then runs the script |
| `requirements.txt` | Pinned Python libraries (`redis==8.1.0`) |
| `.gitignore` | Keeps `venv/` and Python caches out of git |

## Design notes

These notes record the decisions behind the current implementation, so the tool can be rebuilt or changed without rediscovering them.

### Copy method

- Keys are listed with `SCAN` (batches of 1000), not `KEYS`, so a large source isn't blocked.
- Each key is copied with `DUMP` + `PTTL` on the source and `RESTORE <key> <ttl> <payload> REPLACE` on the target. This handles every key type the same way and keeps expiry times.
- `PTTL` of `-1` (no expiry) is restored with ttl `0`. If `DUMP` returns nothing or `PTTL` is `-2`, the key expired during the copy and is skipped and counted.
- The client is redis-py's asyncio API with `decode_responses=False`, because `DUMP` payloads are binary.

### Compatibility

- **RESP2 is forced** (`protocol=2`). redis-py 8 defaults to RESP3, which opens each connection with `HELLO 3`. That command only exists from Redis 6.0, so Redis 5 servers rejected every connection. RESP2 works with all supported versions, and the script needs nothing from RESP3.
- Before copying, both server versions are read with `INFO server` and logged.
- A `RESTORE` error containing `DUMP payload version` means the target is too old. Every key from one source has the same format, so the script stops at the first such error (exit code 3) instead of logging one failure per key.
- The script checks the Python version before importing anything else, so older Pythons get a clear message.

### Error handling

- Connection errors and timeouts stop the whole run (exit code 2). Logging a failure for every remaining key would only add noise.
- Any other Redis error on a single key is logged with the key name, and the copy continues with the next key (exit code 1 at the end).

### Wrapper

- It resolves its own path with `readlink -f`, so it works through symlinks and from any working directory.
- It stores the SHA-256 of `requirements.txt` in `venv/.requirements.sha256`. If the file is missing or the hash differs, it runs `pip install -r requirements.txt`.
- Before creating the virtualenv, it checks that the interpreter exists and is at least Python 3.10.
- It sends its own messages to stderr and starts the script with `exec`, so arguments and exit codes pass through unchanged.

### Tested combinations

| Source | Target | Result |
|---|---|---|
| Redis 7.4.1 | Redis 8.0.5 | 67 keys copied |
| Redis 5.0.14 | Redis 8.0.5 | All key types copied, expiry kept |
| Redis 8.0.5 | Redis 5.0.14 | Stopped at the first key with exit code 3, nothing copied |

### Known limitations

- `--help` shows `usage: redis_copy_urls.py` rather than the wrapper's name.
- Only one database is copied per run. To copy several, run the command once per database.
- Redis Cluster is not supported; the script talks to a single node.
