"""
The SQL the loader sends, checked without a server: every `?` has its
parameter, brackets balance, and statements come in the right order.
"""

from __future__ import annotations

import unittest

from bson import ObjectId

from core import checkpoint as cp
from core.mssql import MssqlConnection
from core.preflight import probe_tables
from core.transfer import plan_column_specs
from tests.fakes import RecordingConnection
from tests.fixtures import documents, plan_for

PLAN, KEY_TYPE = plan_for(documents(120, seed=3), "deep")


def connection(answer=None) -> tuple[MssqlConnection, RecordingConnection]:
    db = MssqlConnection(server="sql", database="app", schema="dbo")
    conn = RecordingConnection(answer)
    db._conn = conn
    return db, conn


def texts(conn: RecordingConnection) -> list[str]:
    return [sql for sql, _ in conn.statements]


class CheckpointSqlTests(unittest.TestCase):
    def test_create_table_is_wrapped_once(self):
        db, conn = connection()
        self.assertTrue(cp.ensure_table(db, "dbo"))
        (sql,) = texts(conn)
        self.assertIn("IF OBJECT_ID(N'[dbo].[Mongo2SqlCheckpoint]', N'U') IS NULL EXEC(N'CREATE TABLE", sql)
        self.assertIn("PRIMARY KEY", sql)

    def test_begin_inserts_when_the_update_found_nothing(self):
        db, conn = connection()
        conn.rowcount = 0
        point = cp.Checkpoint(
            target_table="T", collection="c", mongo_database="d", run_id="r" * 32, mode="full",
            status="running", first_load=True, plan_hash="p" * 64, filter_hash="f" * 64,
            last_id_json=None, last_id_type=None,
        )
        cp.begin(db, "dbo", point)
        update, insert = texts(conn)
        self.assertTrue(update.startswith("UPDATE [dbo].[Mongo2SqlCheckpoint]"))
        self.assertTrue(insert.startswith("INSERT INTO [dbo].[Mongo2SqlCheckpoint]"))
        self.assertEqual(conn.commits, 1)

    def test_advance_checks_ownership(self):
        db, conn = connection()
        args = dict(last_id=ObjectId(), docs_done=10, written=9, rows=40, rejected=1, clipped=0)
        cp.advance(conn.cursor(), "dbo", "T", "r" * 32, **args)
        conn.rowcount = 0
        with self.assertRaises(cp.TakenOver):
            cp.advance(conn.cursor(), "dbo", "T", "r" * 32, **args)
        self.assertEqual(conn.commits, 0)  # part of the batch's transaction

    def test_read_builds_the_row(self):
        oid = ObjectId()
        row = ("T", "c", "d", "r" * 32 + " ", "full", "stopped", 1, "p" * 64, "f" * 64,
               cp.encode_id(oid)[0], "objectid", 5, 4, 20, 1, 0, 2, None, None, None, None)
        db, _ = connection(lambda sql, params: [row] if sql.startswith("SELECT target_table") else None)
        point = cp.read(db, "dbo", "T")
        self.assertEqual(point.run_id, "r" * 32)
        self.assertEqual(point.last_id, oid)
        self.assertTrue(point.first_load)
        self.assertEqual((point.docs_done, point.resume_count), (5, 2))

    def test_finish_and_delete(self):
        db, conn = connection()
        cp.finish(db, "dbo", "T", "r" * 32, "failed", error="x" * 5000)
        self.assertEqual(len(conn.statements[0][1][1]), 4000)
        cp.delete(conn.cursor(), "dbo", "T")
        self.assertIn("DELETE FROM [dbo].[Mongo2SqlCheckpoint] WHERE target_table = ?", texts(conn)[-1])


class MssqlHelperTests(unittest.TestCase):
    def test_delete_keys_casts_to_the_key_type_in_chunks(self):
        db, conn = connection()
        db.delete_keys("dbo", "T", "mongo_id", [f"{i:024x}" for i in range(1203)], chunk=500, key_type="CHAR(24)")
        sizes = [len(params) for _, params in conn.statements]
        self.assertEqual(sizes, [500, 500, 203])
        self.assertIn("CAST(? AS CHAR(24))", texts(conn)[0])

    def test_widen_restates_nullability_and_collation(self):
        db, conn = connection()
        db.widen_column("dbo", "T", "subject", "NVARCHAR(600)", nullable=False, collation="Turkish_CI_AS")
        self.assertEqual(
            texts(conn), ["ALTER TABLE [dbo].[T] ALTER COLUMN [subject] NVARCHAR(600) COLLATE Turkish_CI_AS NOT NULL"]
        )

    def test_applock_and_session(self):
        db, conn = connection(lambda sql, params: [(0,)] if "sp_getapplock" in sql else None)
        db.prepare_session(90_000)
        self.assertTrue(db.get_applock("mongo2sql:dbo.T", 0))
        self.assertIn("SET XACT_ABORT ON; SET LOCK_TIMEOUT 90000;", texts(conn)[0])
        busy, _ = connection(lambda sql, params: [(-1,)] if "sp_getapplock" in sql else None)
        self.assertFalse(busy.get_applock("mongo2sql:dbo.T", 0))

    def test_truncate_drops_and_restores_child_keys_in_one_transaction(self):
        children = ["TA", "TB"]
        keys = [
            ("FK_TA", "dbo", "TA", "CASCADE", "NO_ACTION", 1, "conversation_id", "mongo_id"),
            ("FK_TB", "dbo", "TB", "CASCADE", "NO_ACTION", 1, "conversation_id", "mongo_id"),
        ]

        def answer(sql, params):
            if sql.startswith("SELECT 1 FROM sys.tables"):
                return [(1,)]
            if "FROM sys.foreign_keys" in sql:
                return keys
            return None

        db, conn = connection(answer)
        self.assertEqual(db.truncate_tables("dbo", "T", children), "truncate")
        work = [sql for sql in texts(conn) if not sql.startswith("SELECT")]
        self.assertEqual(
            work,
            [
                "ALTER TABLE [dbo].[TA] DROP CONSTRAINT [FK_TA]",
                "ALTER TABLE [dbo].[TB] DROP CONSTRAINT [FK_TB]",
                "TRUNCATE TABLE [dbo].[TA]",
                "TRUNCATE TABLE [dbo].[TB]",
                "TRUNCATE TABLE [dbo].[T]",
                "ALTER TABLE [dbo].[TA] WITH CHECK ADD CONSTRAINT [FK_TA] FOREIGN KEY ([conversation_id]) "
                "REFERENCES [dbo].[T] ([mongo_id]) ON DELETE CASCADE ON UPDATE NO ACTION",
                "ALTER TABLE [dbo].[TB] WITH CHECK ADD CONSTRAINT [FK_TB] FOREIGN KEY ([conversation_id]) "
                "REFERENCES [dbo].[T] ([mongo_id]) ON DELETE CASCADE ON UPDATE NO ACTION",
            ],
        )

    def test_outside_reference_falls_back_to_deletes(self):
        keys = [("FK_X", "other", "Elsewhere", "NO_ACTION", "NO_ACTION", 1, "t_id", "mongo_id")]

        def answer(sql, params):
            if sql.startswith("SELECT 1 FROM sys.tables"):
                return [(1,)]
            if "FROM sys.foreign_keys" in sql:
                return keys
            return None

        db, conn = connection(answer)
        conn.rowcount = 0  # every DELETE TOP finds the table empty
        self.assertEqual(db.truncate_tables("dbo", "T", ["TA"]), "delete")
        deletes = [sql for sql in texts(conn) if sql.startswith("DELETE TOP")]
        self.assertEqual(deletes, ["DELETE TOP (50000) FROM [dbo].[TA]", "DELETE TOP (50000) FROM [dbo].[T]"])

    def test_column_info_reads_types_and_widenability(self):
        rows = [
            ("mongo_id", "char", 24, 0, 0, 0, "SQL_Latin1_General_CP1_CI_AS", 0, 1, 1),
            ("subject", "nvarchar", 512, 0, 0, 1, "Turkish_CI_AS", 0, 0, 0),
            ("body", "nvarchar", -1, 0, 0, 1, "Turkish_CI_AS", 0, 0, 0),
            ("amount", "decimal", 17, 38, 6, 1, None, 0, 0, 0),
            ("at", "datetime2", 8, 27, 3, 1, None, 0, 0, 0),
        ]
        db, _ = connection(lambda sql, params: rows if "FROM sys.columns c JOIN sys.types" in sql else None)
        info = db.column_info("dbo", "T")
        self.assertEqual(info["mongo_id"]["sql_type"], "CHAR(24)")
        self.assertFalse(info["mongo_id"]["widenable"])
        self.assertEqual(info["subject"]["sql_type"], "NVARCHAR(256)")
        self.assertTrue(info["subject"]["widenable"])
        self.assertEqual(info["body"]["sql_type"], "NVARCHAR(MAX)")
        self.assertEqual(info["amount"]["sql_type"], "DECIMAL(38, 6)")
        self.assertEqual(info["at"]["sql_type"], "DATETIME2(3)")


class ProbeTableTests(unittest.TestCase):
    def test_temp_tables_mirror_the_plan_without_foreign_keys(self):
        tables = probe_tables(PLAN)
        specs = dict(plan_column_specs(PLAN))
        self.assertEqual([table.table for table in tables], list(specs))
        for table in tables:
            with self.subTest(table=table.table):
                self.assertTrue(table.temp.startswith("#"))
                self.assertEqual(table.columns, [name for name, _ in specs[table.table]])
                self.assertIn("PRIMARY KEY (", table.create)
                self.assertNotIn("CONSTRAINT", table.create)
                self.assertNotIn("FOREIGN KEY", table.create)
                self.assertEqual(table.create.count("("), table.create.count(")"))


if __name__ == "__main__":
    unittest.main()
