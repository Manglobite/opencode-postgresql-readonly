# opencode-postgres-readonly

[Русский](README.ru.md) · **English**

Read-only PostgreSQL tools for OpenCode: safe `SELECT`-style queries and a
listing of the configured projects/environments.

## Features

- **`database_exec`** — run a read-only SQL query against PostgreSQL.
- **`database_list`** — list the configured database projects and their
  environments.

The tools are fully self-contained: they share no common code and depend on no
external credential files. All configuration lives in a local `config.json`
next to the source files.

## Install

Copy the repository contents into an OpenCode tool directory:

```bash
# Create the tool directory if it does not exist
mkdir -p ~/.config/opencode/tools/opencode-postgres-readonly

# Copy the source files and the config template (no local config.json)
cp -r database.ts config.ts config.example.json db requirements.txt \
  ~/.config/opencode/tools/opencode-postgres-readonly/
```

Then install the Python dependency:

```bash
pip install -r requirements.txt
# or
pip install "psycopg2-binary>=2.9"
```

### Create the local configuration

Copy the template to `config.json` inside the installation directory and fill
in real values:

```bash
cd ~/.config/opencode/tools/opencode-postgres-readonly
cp config.example.json config.json
chmod 600 config.json
```

### Restart OpenCode

After installing, quit the current OpenCode session and start it again
(`opencode`) so the new tools are picked up.

## Configuration

`config.json` is deliberately ignored by Git. Structure:

```json
{
  "projects": {
    "example-db": {
      "dev": {
        "driver": "postgres",
        "host": "db.example.invalid",
        "port": 5432,
        "database": "exampledb",
        "sslmode": "prefer",
        "auth": {
          "user": "reader",
          "password": "replace_with_real_password"
        }
      }
    }
  }
}
```

| Field | Required | Description |
|-------|:--------:|-------------|
| `driver` | yes | Database type (only `postgres` is supported) |
| `host` | yes | Database host |
| `port` | yes | Port |
| `database` | yes | Database name |
| `sslmode` | yes | SSL mode: `prefer`, `require`, `disable`, `verify-full` |
| `auth.user` | yes | Database user |
| `auth.password` | yes | Password |

Nesting: `projects.<project>.<environment>`. A project may have several
environments (`dev`, `staging`, `prod`). If `environment` is omitted, the
first environment in the list is used. The config is read once and cached in
memory for the lifetime of the process.

> **Note:** `config.json` is not committed (it is in `.gitignore`). Set file
> permissions to `chmod 600`.

## Usage

### database_exec

Runs a read-only SQL query:

```
database_exec({ project: "example-db", environment: "dev", query: "SELECT id, name FROM products LIMIT 5" })
```

Arguments:

| Argument | Required | Description |
|----------|:--------:|-------------|
| `project` | yes | Project name from `config.json` |
| `environment` | no | Environment name; defaults to the first one |
| `query` | yes | A single read-only SQL statement |

Successful response:

```json
{
  "columns": ["id", "name"],
  "rows": [[1, "Product A"], [2, "Product B"]],
  "row_count": 2
}
```

Errors:

```json
{"error": "Query execution failed", "reason": "..."}
{"error": "Connection failed", "reason": "..."}
```

### database_list

Lists projects and their environments:

```
database_list()
```

Response:

```json
{
  "example-db": ["dev", "prod"]
}
```

## Security and limits

- Only `SELECT`, `WITH`, and `EXPLAIN` are allowed as the first keyword.
- All DDL/DML is blocked (`CREATE`, `DROP`, `INSERT`, `UPDATE`, `DELETE`, and
  more).
- System schemas are blocked (`pg_catalog`, `information_schema`, `pg_toast`,
  `pg_temp`).
- Dangerous functions are blocked (`pg_terminate_backend`, `pg_sleep`,
  `lo_*`, `dblink`, and more).
- All `pg_*`-prefixed identifiers are blocked.
- A server-side `READ ONLY` transaction is used.
- `search_path` is forced to `public`.
- Error messages are masked — the real cause does not leak to stdout.
- Maximum query length is 10 000 characters.
- Only a single SQL statement is allowed.
- Backslashes are rejected.
- Credentials are stored only in the local `config.json` (not committed).

## OpenCode permissions

Minimal fragment to allow the tools:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "database_exec": "allow",
    "database_list": "allow"
  }
}
```

For a strict subagent (`"*": "deny"`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "agent": {
    "db-reader": {
      "mode": "subagent",
      "description": "Read-only database access.",
      "permission": {
        "*": "deny",
        "database_exec": "allow",
        "database_list": "allow"
      }
    }
  }
}
```

## License

[MIT](LICENSE)
