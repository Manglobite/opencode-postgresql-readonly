# opencode-postgres-readonly

[Русский](README.ru.md) · **English**

Read-only PostgreSQL tools for OpenCode: restricted `SELECT` queries, schema
metadata and a listing of configured projects/environments for trusted databases.

## Features

- **`database_exec`** — run a restricted read-only SQL query against PostgreSQL.
- **`database_schema`** — read table/column metadata for `public`, without arbitrary SQL or row data (up to 1,000 columns).
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

Install both Python dependencies in the installation directory (Python 3.10+):

```bash
cd ~/.config/opencode/tools/opencode-postgres-readonly
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The tool uses its adjacent `.venv/bin/python` when available, otherwise `python3`. Bun and the OpenCode plugin API must be available in the host environment. For a standalone checkout, install JS dependencies from its own `package.json`, never from a directory without a local manifest.

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
  "row_count": 2,
  "truncated": false
}
```

Errors:

```json
{"error": "Query execution failed", "reason": "..."}
{"error": "Unexpected error", "reason": "An unexpected internal error occurred"}
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

- PostgreSQL AST validation via `pglast`: one SELECT, including nonrecursive read-only CTEs. Unknown constructs fail closed; EXPLAIN, writes, transaction commands, SELECT INTO and row locking are rejected.
- Explicit table schemas must be `public`; system `pg_*` relations are rejected. Schema discovery uses a separate fixed metadata query.
- Only allowlisted built-in functions and types are accepted and qualified with `pg_catalog`. Custom operator qualification is rejected.
- Server-side READ ONLY transaction, rollback on completion, and `search_path=pg_catalog,public`.
- At most 1,000 rows and 1 MiB of serialized JSON per result; `truncated` indicates omitted rows. A server cursor avoids fetching the entire result at once.
- Connection timeout: 5 seconds; statement timeout: 10 seconds per server command (including cursor fetch); lock timeout: 1 second. The TypeScript worker deadline is 25 seconds overall.
- Maximum input SQL length: 10,000 characters. Worker and database errors are masked.
- Install both dependencies from `requirements.txt` in the Python environment used by the tool.

This is not a sandbox for a hostile database schema: views, RLS policies, custom column types, overloaded operators and implicit casts may invoke code indirectly. Use a minimally privileged database role and restrict executable functions; do not grant the agent access to credentials, code modification, or an alternative database client. A single huge field can still consume memory before output truncation. READ ONLY does not undo external side effects.

### Trust boundary

The tool provides restricted reading of a trusted database schema, not a guarantee that arbitrary database code has no side effects. It validates the submitted SQL, not every function hidden behind database objects. READ ONLY blocks ordinary writes in the current transaction even when the account has write privileges; it does not protect other connections, files, or external services.

- Views and row-level security (RLS) policies may call functions indirectly. Custom types, operators and implicit casts can also execute database code.
- Use a dedicated minimally privileged account in production, not a superuser. The agent must not be able to read credentials, modify this tool or bypass it with another database client.
- This is not a data-redaction or authorization layer: readable sensitive rows are returned as requested. Limits apply per call; repeated calls can read more data.
- Timeouts and output limits reduce resource exposure but do not impose a strict database CPU, disk or process-memory quota. A single large field may be allocated before truncation. The 25-second deadline kills the local worker; immediate cancellation of arbitrary external work is not guaranteed.
- SQL support is intentionally incomplete. Recursive CTEs, EXPLAIN, arbitrary functions, custom explicit types/operators and unsupported AST constructs are rejected. Built-in function/type allowlists live in `db/exec.py`.

### Verification performed

**Local automated checks:** 11 Python unit tests passed, along with ESLint, Ruff, TypeScript type checking and `git diff --check`. Tests cover accepted SQL, rejected constructs, pre-connection validation, row/byte limits, cleanup and schema discovery; database interactions in these tests are mocked.

**Live integration run:** 23 checks passed on PostgreSQL 16.13 in Docker using a superuser account and a separate disposable database. Checked SELECT, CTE/JOIN/aggregation, date/numeric/UUID results, empty results, schema discovery, 1,000-row truncation and the 1 MiB output limit. Statement and lock timeouts were observed at approximately 10 and 1 seconds. Fixture contents remained unchanged after all attack checks; the disposable database was removed.

Attack categories checked (names only):

- Direct DELETE, DROP and TRUNCATE.
- Multi-statement injection using dollar-sign identifiers and transaction escape.
- Data-modifying CTE.
- Quoted-function/comment filter bypass and transaction-setting changes.
- Unicode system-catalog identifier bypass.
- Cross-connection write function call.
- Direct application-function call.
- Row locking, SELECT INTO and EXPLAIN ANALYZE.
- Indirect write through a view: rejected by PostgreSQL READ ONLY.

The live run exercised the Python executor, not the complete OpenCode-to-TypeScript path. Direct attack calls were rejected before execution; this does not prove safety of all extension functions. The 25-second TypeScript deadline, connection timeout under network failure, RLS/custom-type/operator side effects, external side effects and other PostgreSQL versions were not integration-tested. The one-off integration harness is not included in the repository; `bun run test` runs only the unit suite. These results are evidence for the tested scenarios, not a security certification.

### Development checks

Run from this repository root, which contains its own `package.json`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
bun install --frozen-lockfile
bun run lint
bun run typecheck
bun run test
```

`typecheck` checks TypeScript only. Keep `config.json`, virtual environments, dependency directories and local test data out of commits; publish source and lock files, not a full directory archive.

## OpenCode permissions

Minimal fragment to allow the tools:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "database_schema": "allow",
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
        "database_schema": "allow",
        "database_exec": "allow",
        "database_list": "allow"
      }
    }
  }
}
```

## License

[MIT](LICENSE)
