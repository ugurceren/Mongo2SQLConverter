"""Column names in a plan are unique the way SQL Server compares them."""

from __future__ import annotations

import unittest

import bson
from bson import ObjectId

from core.convert import Flattener
from core.inspect import (
    NESTING_DEEP,
    Profile,
    build_plan,
    ddl_statements,
    detect_map_prefixes,
    key_column_type,
)


def plan_of(docs, nesting=NESTING_DEEP):
    profile = Profile()
    for doc in docs:
        profile.add_document(doc)
    return build_plan(profile, "orders", "dbo", detect_map_prefixes(profile, 30, 0.2), 1.5, nesting=nesting)


DOCS = [
    bson.decode(
        bson.encode(
            {
                "_id": ObjectId(),
                "city": "Ankara",
                "City": "ANKARA",
                "mongo_id": "legacy-key",
                "items": [{"sku": "a", "order_id": 7, "items_idx": 99, "Sku": "A"}],
            }
        )
    )
    for _ in range(20)
]


class PlanNameTests(unittest.TestCase):
    def test_names_differing_only_by_case_get_a_suffix(self):
        plan = plan_of(DOCS)
        names = [column["name"] for column in plan["root"]["columns"]]
        self.assertEqual(len({name.lower() for name in names}), len(names), names)
        self.assertIn("city", names)
        self.assertIn("City_x", names)

    def test_key_and_index_columns_are_never_taken(self):
        plan = plan_of(DOCS)
        root = {column["path"]: column["name"] for column in plan["root"]["columns"]}
        self.assertEqual(root["_id"], "mongo_id")
        self.assertEqual(root["mongo_id"], "mongo_id_x")
        child = plan["children"][0]
        reserved = {child["parent_key"].lower(), *(name.lower() for name in child["idx_columns"])}
        for column in child["columns"]:
            self.assertNotIn(column["name"].lower(), reserved, column)

    def test_ddl_has_no_duplicate_columns(self):
        for statement in (sql for _, sql in ddl_statements(plan_of(DOCS))):
            names = [line.split("]", 1)[0].strip(" [").lower() for line in statement.splitlines()[1:] if line.strip().startswith("[")]
            self.assertEqual(len(names), len(set(names)), statement)

    def test_values_land_in_their_own_columns(self):
        plan = plan_of(DOCS)
        flat = Flattener(plan, key_column_type(plan)).flatten(DOCS[0])
        by_name = {column["name"]: flat.root[i] for i, column in enumerate(plan["root"]["columns"])}
        self.assertEqual((by_name["city"], by_name["City_x"], by_name["mongo_id_x"]), ("Ankara", "ANKARA", "legacy-key"))
        self.assertEqual(by_name["mongo_id"], str(DOCS[0]["_id"]))


if __name__ == "__main__":
    unittest.main()
