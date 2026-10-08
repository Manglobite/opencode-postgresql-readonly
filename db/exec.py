#!/usr/bin/env python3
import datetime
import decimal
import json
import sys
import uuid

import psycopg2
from pglast import ast, parse_sql
from pglast.stream import RawStream
from pglast.visitors import Visitor


MAX_QUERY_LENGTH = 10000
MAX_ROWS = 1000
MAX_RESULT_BYTES = 1024 * 1024
ALLOWED_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "bool_and", "bool_or", "every",
    "abs", "ceil", "ceiling", "floor", "round", "trunc", "mod", "sqrt",
    "lower", "upper", "length", "char_length", "octet_length", "trim",
    "btrim", "ltrim", "rtrim", "substring", "substr", "replace", "concat",
    "concat_ws", "left", "right", "strpos", "split_part", "starts_with",
    "date_trunc", "date_part", "extract", "age", "now", "to_char",
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value",
    "last_value", "nth_value", "ntile", "percent_rank", "cume_dist",
})
ALLOWED_TYPES = frozenset({
    "bool", "boolean", "int2", "int4", "int8", "smallint", "integer",
    "bigint", "numeric", "decimal", "float4", "float8", "real", "text",
    "varchar", "bpchar", "date", "time", "timetz", "timestamp",
    "timestamptz", "interval", "uuid", "json", "jsonb", "bytea",
})
ALLOWED_OPERATORS = frozenset({
    "=", "<>", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%",
    "^", "||", "~~", "!~~", "~~*", "!~~*", "~", "!~", "~*", "!~*",
    "->", "->>", "#>", "#>>", "@>", "<@", "?", "?|", "?&",
})
ALLOWED_NODES = frozenset({
    "SelectStmt", "ResTarget", "ColumnRef", "A_Star", "A_Const", "String",
    "Integer", "Float", "Boolean", "BitString", "RangeVar", "Alias",
    "JoinExpr", "RangeSubselect", "WithClause", "CommonTableExpr", "SubLink",
    "A_Expr", "BoolExpr", "NullTest", "BooleanTest", "FuncCall", "TypeCast",
    "TypeName", "CoalesceExpr", "MinMaxExpr", "CaseExpr", "CaseWhen",
    "SortBy", "WindowDef", "SQLValueFunction", "RowExpr", "A_ArrayExpr",
    "A_Indirection", "A_Indices", "GroupingSet", "GroupingFunc",
})


class SqlToolError(Exception):
    pass


def load_request() -> tuple[str, dict, str]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SqlToolError("Invalid JSON input") from exc
    if not isinstance(payload, dict):
        raise SqlToolError("Input must be an object")
    query = payload.get("query")
    config = payload.get("config")
    operation = payload.get("operation", "query")
    if operation not in {"query", "schema"}:
        raise SqlToolError("Unsupported operation")
    if operation == "schema":
        if "query" in payload:
            raise SqlToolError("Schema discovery does not accept SQL")
        query = "__schema__"
    if not isinstance(query, str) or not query.strip():
        raise SqlToolError("The 'query' field must be a non-empty string.")
    if not isinstance(config, dict):
        raise SqlToolError("The 'config' field must be an object.")
    required_keys = ["host", "port", "database", "user", "password"]
    if any(key not in config for key in required_keys):
        raise SqlToolError("Missing database configuration fields")
    return query, config, operation


def normalized_ast(value):
    if isinstance(value, (list, tuple)):
        return [normalized_ast(item) for item in value]
    if isinstance(value, dict):
        if "#" in value:
            return value["name"]
        fields = {key: normalized_ast(item) for key, item in value.items() if key != "@" and item is not None}
        return {value["@"]: fields} if "@" in value else fields
    return value


def names(items: list) -> list[str]:
    return [item["String"]["sval"] for item in items]


def check_ast(value) -> None:
    if isinstance(value, list):
        for item in value:
            check_ast(item)
        return
    if not isinstance(value, dict):
        return
    for key, node in value.items():
        if key[:1].isupper():
            if key not in ALLOWED_NODES:
                raise SqlToolError(f"Unsupported SQL construct: {key}")
            if key == "SelectStmt" and (node.get("intoClause") or node.get("lockingClause")):
                raise SqlToolError("SELECT INTO and row locking are not allowed")
            if key == "RangeVar":
                if node.get("catalogname") or node.get("schemaname") not in {None, "public"}:
                    raise SqlToolError("Only the public application schema is allowed")
                if node["relname"].lower().startswith("pg_"):
                    raise SqlToolError("System objects are not allowed")
            if key == "FuncCall":
                function = names(node["funcname"])
                if not function or function[-1] not in ALLOWED_FUNCTIONS or (
                    len(function) != 1 and not (len(function) == 2 and function[0] == "pg_catalog")
                ):
                    raise SqlToolError("Function is not in the allowed built-in function list")
            if key == "TypeName":
                typename = names(node.get("names", []))
                if not typename or typename[-1] not in ALLOWED_TYPES or (
                    len(typename) != 1 and not (len(typename) == 2 and typename[0] == "pg_catalog")
                ):
                    raise SqlToolError("Only built-in types are allowed")
            if key == "A_Expr":
                operators = names(node.get("name", []))
                allowed = ALLOWED_OPERATORS | {"BETWEEN", "NOT BETWEEN", "BETWEEN SYMMETRIC", "NOT BETWEEN SYMMETRIC"}
                if operators and (len(operators) != 1 or operators[0] not in allowed):
                    raise SqlToolError("Operator is not allowed")
            if key == "SortBy" and node.get("useOp"):
                raise SqlToolError("Custom sort operators are not allowed")
            if key == "WithClause" and node.get("recursive"):
                raise SqlToolError("Recursive CTEs are not supported")
        check_ast(node)


class QualifyBuiltins(Visitor):
    def visit_FuncCall(self, ancestors, node):
        if len(node.funcname) == 1:
            node.funcname = (ast.String(sval="pg_catalog"), *node.funcname)

    def visit_TypeName(self, ancestors, node):
        if len(node.names) == 1:
            node.names = (ast.String(sval="pg_catalog"), *node.names)


def validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise SqlToolError("SQL query is empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise SqlToolError("Query length exceeds 10000 characters")
    try:
        tree = parse_sql(query)
        if len(tree) != 1 or not isinstance(tree[0].stmt, ast.SelectStmt):
            raise SqlToolError("Only a single SELECT or read-only WITH statement is allowed")
        check_ast(normalized_ast(tree[0].stmt()))
        QualifyBuiltins()(tree)
        return RawStream()(tree)
    except SqlToolError:
        raise
    except Exception as exc:
        raise SqlToolError("Invalid or unsupported SQL") from exc


def json_value(value):
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta, decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    raise TypeError("Unsupported result type")


def encode_result(result: dict) -> str:
    return json.dumps(result, ensure_ascii=True, default=json_value, separators=(",", ":"))


def bounded_result(cur, schema: bool = False) -> dict:
    result = {
        "columns": [desc[0] for desc in cur.description],
        "rows": [], "row_count": 0, "truncated": False,
    }
    if schema:
        result["schema"] = "public"
    reserved = len(encode_result(result).encode("utf-8")) + 32
    if reserved > MAX_RESULT_BYTES:
        raise SqlToolError("Result column metadata exceeds the output limit")
    size = reserved
    for _ in range(MAX_ROWS + 1):
        row = cur.fetchone()
        if row is None:
            break
        if len(result["rows"]) == MAX_ROWS:
            result["truncated"] = True
            break
        row = list(row)
        row_size = len(encode_result(row).encode("utf-8")) + 1
        if size + row_size > MAX_RESULT_BYTES:
            result["truncated"] = True
            break
        result["rows"].append(row)
        size += row_size
    result["row_count"] = len(result["rows"])
    return result


SCHEMA_QUERY = """
SELECT c.table_schema, c.table_name, t.table_type,
       c.column_name, c.data_type, c.udt_name,
       c.is_nullable, c.ordinal_position
FROM information_schema.columns AS c
JOIN information_schema.tables AS t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = %s
ORDER BY c.table_name, c.ordinal_position
LIMIT 1001
"""


def execute_query(config: dict, query: str, operation: str = "query") -> dict:
    if operation not in {"query", "schema"}:
        raise SqlToolError("Unsupported operation")
    schema = operation == "schema"
    safe_query = None if schema else validate_query(query)
    conn = None
    try:
        try:
            conn = psycopg2.connect(
                host=config["host"], port=config["port"], dbname=config["database"],
                user=config["user"], password=config["password"],
                sslmode=config.get("sslmode", "prefer"), connect_timeout=5,
                options="-c search_path=pg_catalog,public -c default_transaction_read_only=on "
                        "-c statement_timeout=10000 -c lock_timeout=1000 "
                        "-c idle_in_transaction_session_timeout=15000",
            )
            conn.set_session(readonly=True, autocommit=False)
        except psycopg2.OperationalError as exc:
            raise SqlToolError("Connection refused or invalid credentials") from exc
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL search_path = pg_catalog, public")
                cur.execute("SET LOCAL statement_timeout = '10s'")
                cur.execute("SET LOCAL lock_timeout = '1s'")
            with conn.cursor(name="readonly_result") as cur:
                cur.itersize = 1
                if schema:
                    cur.execute(SCHEMA_QUERY, ("public",))
                else:
                    cur.execute(f"SELECT * FROM ({safe_query}) AS readonly_result LIMIT {MAX_ROWS + 1}")
                first = cur.fetchone()
                return bounded_result(PrefetchedCursor(cur, first), schema)
        except psycopg2.Error as exc:
            raise SqlToolError("Query execution failed or exceeded its time limit") from exc
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


class PrefetchedCursor:
    def __init__(self, cursor, first):
        self.cursor = cursor
        self.description = cursor.description
        self.first = first
        self.pending = True

    def fetchone(self):
        if self.pending:
            self.pending = False
            return self.first
        return self.cursor.fetchone()


def main() -> None:
    try:
        query, config, operation = load_request()
        result = execute_query(config, query, operation)
        print(encode_result(result))
    except SqlToolError as exc:
        print(encode_result({"error": "Query execution failed", "reason": str(exc)}))
    except Exception:
        print(encode_result({"error": "Unexpected error", "reason": "An unexpected internal error occurred"}))


if __name__ == "__main__":
    main()
