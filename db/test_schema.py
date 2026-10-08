import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("executor", Path(__file__).with_name("exec.py"))
executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(executor)
CONFIG = {"host": "fixture", "port": 5432, "database": "fixture", "user": "fixture", "password": "fixture"}


class SchemaTests(unittest.TestCase):
    def test_system_queries_remain_blocked(self):
        for query in ["SELECT * FROM information_schema.columns", "SELECT * FROM pg_catalog.pg_class", "__schema__"]:
            with self.assertRaises(executor.SqlToolError):
                executor.validate_query(query)

    def test_schema_rejects_custom_sql(self):
        with patch("sys.stdin", io.StringIO(json.dumps({"operation": "schema", "query": "SELECT 1", "config": CONFIG}))):
            with self.assertRaises(executor.SqlToolError):
                executor.load_request()

    def test_schema_operation_is_explicit(self):
        with patch("sys.stdin", io.StringIO(json.dumps({"operation": "schema", "config": CONFIG}))):
            self.assertEqual(executor.load_request(), ("__schema__", CONFIG, "schema"))
        with patch("sys.stdin", io.StringIO(json.dumps({"query": "SELECT 1", "config": CONFIG}))):
            self.assertEqual(executor.load_request(), ("SELECT 1", CONFIG, "query"))

    def test_metadata_bounded_readonly(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.description = [("table_name",)]
        cursor.fetchone.side_effect = [("fixture",)] * 1001 + [None]
        with patch.object(executor.psycopg2, "connect", return_value=conn):
            result = executor.execute_query(CONFIG, "__schema__", "schema")
        conn.set_session.assert_called_once_with(readonly=True, autocommit=False)
        sql, params = cursor.execute.call_args.args
        self.assertIn("LIMIT 1001", sql)
        self.assertEqual(params, ("public",))
        self.assertEqual(result["row_count"], 1000)
        self.assertTrue(result["truncated"])
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()


class SecurityTests(unittest.TestCase):
    def test_rejects_unsafe_queries(self):
        queries = [
            "DELETE FROM t", "SELECT 1; SELECT 2", "EXPLAIN SELECT 1",
            "SELECT 1 AS a$$; COMMIT; BEGIN READ WRITE; DELETE FROM t; COMMIT; SELECT 1 AS b$$",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "SELECT * INTO t FROM source", "SELECT * FROM t FOR UPDATE",
            "SELECT * FROM t FOR SHARE", "SELECT public.custom_side_effect_function()",
            "SELECT dblink_exec('dbname=fixture', 'DELETE FROM t')",
            '''SELECT "set_config"/**/('statement_timeout', '0', false)''',
            r'SELECT * FROM U&"\0070g_catalog".U&"\0070g_class"',
            "SELECT * FROM pg_class", "SELECT * FROM private.t",
            "SELECT 'x'::public.custom_type", "SELECT 'x'::regclass",
            "SELECT 1 OPERATOR(public.+) 2", "SELECT * FROM generate_series(1, 10)",
            "SELECT 1 ORDER BY 1 USING OPERATOR(public.<)",
            "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
            "SELECT 'x' COLLATE public.custom_collation", "SELECT pg_sleep(10)",
        ]
        for query in queries:
            with self.subTest(query=query), self.assertRaises(executor.SqlToolError):
                executor.validate_query(query)

    def test_allows_read_queries(self):
        queries = [
            "SELECT 1", "SELECT '; DELETE FROM t'", "SELECT 1 AS a$$",
            "SELECT $$hello; world$$", "SELECT id FROM public.t WHERE id IN (1, 2)",
            "SELECT * FROM t WHERE id BETWEEN 1 AND 5 AND name LIKE 'a%'",
            "WITH x AS (SELECT id FROM public.t) SELECT count(*) FROM x",
            "SELECT a.id, sum(b.amount) FROM a JOIN b ON a.id=b.id GROUP BY a.id HAVING count(*)>1",
            "SELECT coalesce(name, 'unknown'), CASE WHEN id>0 THEN 1 ELSE 0 END FROM t",
            "SELECT id::numeric, row_number() OVER (ORDER BY id) FROM t LIMIT 3",
            "SELECT 1 UNION ALL SELECT 2", "SELECT extract(year FROM CURRENT_DATE)",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(executor.validate_query(query))
        self.assertIn("pg_catalog.count", executor.validate_query("SELECT count(*) FROM t"))

    def test_rejects_before_connection(self):
        with patch.object(executor.psycopg2, "connect") as connect:
            with self.assertRaises(executor.SqlToolError):
                executor.execute_query(CONFIG, "SELECT 1; COMMIT")
            connect.assert_not_called()

    def test_query_limits_and_cleanup(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.description = [("id",)]
        cursor.fetchone.side_effect = [(1,), None]
        with patch.object(executor.psycopg2, "connect", return_value=conn) as connect:
            result = executor.execute_query(CONFIG, "SELECT id FROM public.t")
        self.assertEqual(result["rows"], [[1]])
        self.assertFalse(result["truncated"])
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 5)
        self.assertIn("statement_timeout=10000", connect.call_args.kwargs["options"])
        self.assertIn("LIMIT 1001", cursor.execute.call_args.args[0])
        cursor.fetchall.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_cleanup_on_server_error(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = executor.psycopg2.Error("private details")
        with patch.object(executor.psycopg2, "connect", return_value=conn):
            with self.assertRaisesRegex(executor.SqlToolError, "Query execution failed"):
                executor.execute_query(CONFIG, "SELECT 1")
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_byte_limit(self):
        cursor = MagicMock()
        cursor.description = [("text",)]
        cursor.fetchone.side_effect = [("a",), ("я" * executor.MAX_RESULT_BYTES,), None]
        result = executor.bounded_result(cursor)
        self.assertEqual(result["rows"], [["a"]])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(executor.encode_result(result).encode()), executor.MAX_RESULT_BYTES)

    def test_exact_row_limit_and_empty(self):
        for count in (0, 1000, 1001):
            with self.subTest(count=count):
                cursor = MagicMock()
                cursor.description = [("id",)]
                cursor.fetchone.side_effect = [(1,)] * count + [None]
                result = executor.bounded_result(cursor)
                self.assertEqual(result["row_count"], min(count, 1000))
                self.assertEqual(result["truncated"], count > 1000)


if __name__ == "__main__":
    unittest.main()
