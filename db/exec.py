#!/usr/bin/env python3
"""
PostgreSQL read-only query executor.
Uses psycopg2 with server-enforced READ ONLY transaction.
"""
import json
import re
import sys

import psycopg2


ALLOWED_FIRST_KEYWORDS = {"SELECT", "WITH", "EXPLAIN"}
DISALLOWED_KEYWORDS = {
    "ALTER",
    "ANALYZE",
    "CALL",
    "COPY",
    "CREATE",
    "DELETE",
    "DISCARD",
    "DROP",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "LISTEN",
    "LOCK",
    "NOTIFY",
    "REINDEX",
    "REVOKE",
    "TRUNCATE",
    "UNLISTEN",
    "UPDATE",
    "VACUUM",
}
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast", "pg_temp"}
MAX_QUERY_LENGTH = 10000

FORBIDDEN_FUNCTIONS = [
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_logdir_ls",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_advisory_unlock",
    "pg_advisory_unlock_shared",
    "pg_advisory_unlock_all",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "lo_import",
    "lo_export",
    "lo_create",
    "lo_unlink",
    "lo_open",
    "lo_close",
    "lo_read",
    "lo_write",
    "lo_lseek",
    "lo_tell",
    "lo_truncate",
    "set_config",
    "current_setting",
    "pg_current_logfile",
    "dblink_connect",
    "dblink_connect_u",
    "dblink_disconnect",
    "dblink",
    "pg_export_snapshot",
    "pg_create_restore_point",
    "pg_switch_wal",
    "pg_promote",
    "pg_log_backend_memory_contexts",
]


class SqlToolError(Exception):
    pass


def load_request() -> tuple[str, dict]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SqlToolError(f"Invalid JSON input: {exc}") from exc

    query = payload.get("query")
    config = payload.get("config")

    if not isinstance(query, str) or not query.strip():
        raise SqlToolError("The 'query' field must be a non-empty string.")
    if not isinstance(config, dict):
        raise SqlToolError("The 'config' field must be an object.")

    required_keys = ["host", "port", "database", "user", "password"]
    missing = [key for key in required_keys if key not in config]
    if missing:
        raise SqlToolError(f"Missing config keys: {', '.join(missing)}")

    return query, config


def is_identifier_char(char: str) -> bool:
    return char.isalnum() or char in {"_", "$"}


def remove_comments_and_strings(sql: str) -> str:
    result: list[str] = []
    i = 0
    length = len(sql)
    block_comment_depth = 0
    dollar_tag: str | None = None

    while i < length:
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                result.append(" ")
                i += len(dollar_tag)
                dollar_tag = None
            else:
                i += 1
            continue

        if block_comment_depth > 0:
            if sql.startswith("/*", i):
                block_comment_depth += 1
                i += 2
            elif sql.startswith("*/", i):
                block_comment_depth -= 1
                i += 2
            else:
                i += 1
            continue

        if sql.startswith("--", i):
            while i < length and sql[i] != "\n":
                i += 1
            result.append("\n")
            continue

        if sql.startswith("/*", i):
            block_comment_depth = 1
            i += 2
            continue

        if sql[i] == "'":
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            result.append(" ")
            continue

        if sql[i] == '"':
            i += 1
            while i < length:
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            result.append(" ")
            continue

        if sql[i] == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                result.append(" ")
                i += len(dollar_tag)
                continue

        result.append(sql[i])
        i += 1

    if block_comment_depth > 0:
        raise SqlToolError("Unterminated block comment in SQL query.")
    if dollar_tag is not None:
        raise SqlToolError("Unterminated dollar-quoted string in SQL query.")

    return "".join(result)


def split_top_level_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    depth = 0

    for char in sql:
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == ";" and depth == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)

    return statements


def extract_words(sql: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []

    for char in sql:
        if is_identifier_char(char):
            current.append(char)
            continue
        if current:
            words.append("".join(current).upper())
            current = []

    if current:
        words.append("".join(current).upper())

    return words


def validate_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise SqlToolError("SQL query is empty.")

    if len(normalized) > MAX_QUERY_LENGTH:
        raise SqlToolError("Query length exceeds 10000 characters.")

    stripped = remove_comments_and_strings(normalized)

    if "\\" in stripped:
        raise SqlToolError("Query contains invalid characters (backslash).")

    # Check for unquoted system schema references in cleaned SQL
    for schema in SYSTEM_SCHEMAS:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(schema) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        if pattern.search(stripped):
            raise SqlToolError(f"Access to system schema '{schema}' is not allowed.")

    # Check for quoted system schema references in original SQL
    for schema in SYSTEM_SCHEMAS:
        pattern = re.compile(
            r'"\s*' + re.escape(schema) + r'\s*"',
            re.IGNORECASE,
        )
        if pattern.search(normalized):
            raise SqlToolError(f"Access to system schema '{schema}' is not allowed.")

    for func in FORBIDDEN_FUNCTIONS:
        pattern = re.compile(
            r'(?<![A-Za-z0-9_])' + re.escape(func) + r'\s*\(',
            re.IGNORECASE,
        )
        if pattern.search(stripped):
            raise SqlToolError(f"Function '{func}' is not allowed in read-only mode")

    # Check for quoted pg_* identifiers in original SQL
    if re.search(r'"\s*pg_[A-Za-z0-9_]*\s*"', normalized, re.IGNORECASE):
        raise SqlToolError("Access to system objects (pg_*) is not allowed")

    # Check for quoted forbidden functions in original SQL
    for func in FORBIDDEN_FUNCTIONS:
        pattern = re.compile(
            r'"\s*' + re.escape(func) + r'\s*"\s*\(',
            re.IGNORECASE,
        )
        if pattern.search(normalized):
            raise SqlToolError(f"Function '{func}' is not allowed in read-only mode")

    statements = split_top_level_statements(stripped)

    if not statements:
        raise SqlToolError("SQL query is empty after removing comments.")
    if len(statements) != 1:
        raise SqlToolError("Only a single SQL statement is allowed.")

    statement = statements[0]
    words = extract_words(statement)
    if not words:
        raise SqlToolError("Could not detect a SQL statement.")

    # Block all pg_* prefixed identifiers (system catalog objects)
    for word in words:
        if word.startswith("PG_"):
            raise SqlToolError(
                f"Access to system objects (pg_*) is not allowed: '{word}'"
            )

    first_keyword = words[0]
    if first_keyword not in ALLOWED_FIRST_KEYWORDS:
        raise SqlToolError(
            f"Only {'/'.join(sorted(ALLOWED_FIRST_KEYWORDS))} queries are allowed."
        )

    dangerous = sorted({word for word in words if word in DISALLOWED_KEYWORDS})
    if dangerous:
        raise SqlToolError(
            "Only read-only queries are allowed. Disallowed keywords found: "
            + ", ".join(dangerous)
        )

    # Check for quoted disallowed keywords in original SQL
    for kw in DISALLOWED_KEYWORDS:
        pattern = re.compile(
            r'"\s*' + re.escape(kw) + r'\s*"',
            re.IGNORECASE,
        )
        if pattern.search(normalized):
            raise SqlToolError(
                "Only read-only queries are allowed. Disallowed keyword found: " + kw
            )

    return normalized.rstrip().rstrip(";")


def execute_query(config: dict, query: str) -> dict:
    conn = None
    try:
        try:
            conn = psycopg2.connect(
                host=config["host"],
                port=config["port"],
                dbname=config["database"],
                user=config["user"],
                password=config["password"],
                sslmode=config.get("sslmode", "prefer"),
                options="-c search_path=public,pg_catalog -c default_transaction_read_only=on",
            )
            conn.set_session(readonly=True, autocommit=False)
        except psycopg2.OperationalError:
            raise SqlToolError("Connection refused or invalid credentials")

        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path = 'public'")
                cur.execute(query)
                if cur.description:
                    columns = [desc[0] for desc in cur.description]
                    rows = [list(row) for row in cur.fetchall()]
                    row_count = len(rows)
                else:
                    columns = []
                    rows = []
                    row_count = 0
        except psycopg2.Error as e:
            raise SqlToolError("Query execution failed")

        return {"columns": columns, "rows": rows, "row_count": row_count}

    finally:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def main() -> None:
    try:
        query, config = load_request()
        safe_query = validate_query(query)
        result = execute_query(config, safe_query)
        print(json.dumps(result))
    except SqlToolError as exc:
        error_msg = str(exc)
        if "Connection refused" in error_msg or "invalid credentials" in error_msg:
            output = {"error": "Connection failed", "reason": error_msg}
        else:
            output = {"error": "Query execution failed", "reason": error_msg}
        print(json.dumps(output))
    except Exception:
        print(json.dumps({"error": "Unexpected error", "reason": "An unexpected internal error occurred"}))


if __name__ == "__main__":
    main()
