"""DRDL output follows mongodrdl's format (the BI Connector export)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import yaml
from bson import ObjectId

from core.inspect import Profile, drdl_tables, render_database_drdl


def tables_of(docs, collection):
    profile = Profile()
    for doc in docs:
        profile.add_document(doc)
    return drdl_tables(profile, collection)


def poly(name, keys):
    """The `$addFields` stage mongodrdl writes for an object-or-scalar field."""

    def cond(when_object, otherwise):
        return {"$cond": [{"$eq": [{"$type": "$" + name}, "object"]}, when_object, otherwise]}

    fields = {name: cond(None, "$" + name)}
    for key in keys:
        fields[f"{name}_DOT_{key}"] = cond(f"${name}.{key}", None)
    return {"$addFields": fields}


def col(name, mongo="string", sql="varchar", sql_name=None):
    return {"Name": name, "MongoType": mongo, "SqlName": sql_name or name.replace("_DOT_", "."), "SqlType": sql}


FILTER = col("conversations_filter", "mongo.Filter")
ID = col("_id")
LOCATION = poly("tracker.latest_message.metadata.location", ["latitude", "longitude"])
EVENTS_UNWIND = {
    "$unwind": {
        "includeArrayIndex": "tracker.events_idx",
        "path": "$tracker.events",
        "preserveNullAndEmptyArrays": False,
    }
}
CUSTOM = poly("tracker.events.data.custom", ["responses"])

CONVERSATIONS = [
    {
        "_id": "c1",
        "__v": 3,
        "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "actions": ["greet", "bye"],
        "tracker": {
            "latest_message": {"metadata": {"location": {"latitude": 41.0, "longitude": 29.0}}},
            "events": [
                {
                    "confidence": 0.9,
                    "data": {"custom": {"responses": [{"channel": "web", "metadata": {"response": "x"}}]}},
                    "value": [{"channelId": "a"}, "plain"],
                    "entities": [{"processors": ["p1"]}],
                },
                {"confidence": 1, "data": {"custom": "text"}, "value": None},
            ],
        },
    },
    {
        "_id": "c2",
        "__v": 4,
        "createdAt": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "actions": [],
        "tracker": {"latest_message": {"metadata": {"location": "unknown"}}, "events": []},
    },
]


class ConversationsTest(unittest.TestCase):
    def setUp(self):
        self.tables = {t["table"]: t for t in tables_of(CONVERSATIONS, "conversations")}

    def test_one_table_per_array_level_sorted(self):
        self.assertEqual(
            list(self.tables),
            [
                "conversations",
                "conversations_actions",
                "conversations_tracker_events",
                "conversations_tracker_events_data_custom_responses",
                "conversations_tracker_events_entities",
                "conversations_tracker_events_entities_processors",
                "conversations_tracker_events_value",
            ],
        )

    def test_root_flattens_objects_and_splits_polymorphic_fields(self):
        root = self.tables["conversations"]
        self.assertEqual(root["collection"], "conversations")
        self.assertEqual(root["pipeline"], [LOCATION])
        self.assertEqual(
            root["columns"],
            [
                ID,
                col("__v", "int", "int"),
                FILTER,
                col("createdAt", "date", "timestamp"),
                col("tracker.latest_message.metadata.location"),
                col("tracker.latest_message.metadata.location_DOT_latitude"),
                col("tracker.latest_message.metadata.location_DOT_longitude"),
            ],
        )

    def test_scalar_array(self):
        actions = self.tables["conversations_actions"]
        self.assertEqual(
            actions["pipeline"],
            [
                LOCATION,
                {"$unwind": {"includeArrayIndex": "actions_idx", "path": "$actions", "preserveNullAndEmptyArrays": False}},
            ],
        )
        self.assertEqual(actions["columns"], [ID, col("actions"), col("actions_idx", "int", "int"), FILTER])

    def test_array_of_objects_carries_only_its_own_fields(self):
        events = self.tables["conversations_tracker_events"]
        self.assertEqual(events["pipeline"], [LOCATION, EVENTS_UNWIND, CUSTOM])
        self.assertEqual(
            events["columns"],
            [
                ID,
                FILTER,
                col("tracker.events.confidence", "float64", "float"),
                col("tracker.events.data.custom"),
                col("tracker.events_idx", "int", "int"),
            ],
        )

    def test_array_under_split_object_unwinds_the_alias(self):
        responses = self.tables["conversations_tracker_events_data_custom_responses"]
        self.assertEqual(
            responses["pipeline"],
            [
                LOCATION,
                EVENTS_UNWIND,
                CUSTOM,
                {"$match": {"tracker.events.data.custom_DOT_responses": {"$ne": []}}},
                {
                    "$unwind": {
                        "includeArrayIndex": "tracker.events.data.custom.responses_idx",
                        "path": "$tracker.events.data.custom_DOT_responses",
                        "preserveNullAndEmptyArrays": True,
                    }
                },
            ],
        )
        self.assertEqual(
            responses["columns"],
            [
                ID,
                FILTER,
                col("tracker.events.data.custom_DOT_responses.channel"),
                col("tracker.events.data.custom_DOT_responses.metadata.response"),
                col("tracker.events.data.custom.responses_idx", "int", "int"),
                col("tracker.events_idx", "int", "int"),
            ],
        )

    def test_nested_arrays_unwind_each_level(self):
        processors = self.tables["conversations_tracker_events_entities_processors"]
        unwinds = [s["$unwind"] for s in processors["pipeline"] if "$unwind" in s]
        self.assertEqual(
            [(u["path"], u["includeArrayIndex"]) for u in unwinds],
            [
                ("$tracker.events", "tracker.events_idx"),
                ("$tracker.events.entities", "tracker.events.entities_idx"),
                ("$tracker.events.entities.processors", "tracker.events.entities.processors_idx"),
            ],
        )
        self.assertEqual(
            [c["Name"] for c in processors["columns"]],
            [
                "_id",
                "conversations_filter",
                "tracker.events.entities.processors",
                "tracker.events.entities.processors_idx",
                "tracker.events.entities_idx",
                "tracker.events_idx",
            ],
        )

    def test_sometimes_null_array_with_mixed_elements(self):
        value = self.tables["conversations_tracker_events_value"]
        self.assertEqual(
            value["pipeline"][-3:],
            [
                {"$match": {"tracker.events.value": {"$ne": []}}},
                {
                    "$unwind": {
                        "includeArrayIndex": "tracker.events.value_idx",
                        "path": "$tracker.events.value",
                        "preserveNullAndEmptyArrays": True,
                    }
                },
                poly("tracker.events.value", ["channelId"]),
            ],
        )
        self.assertIn(col("tracker.events.value_DOT_channelId"), value["columns"])


class RenderTest(unittest.TestCase):
    def test_yaml_matches_mongodrdl_layout(self):
        votes = tables_of([{"_id": ObjectId(), "vote": True, "metadata": {"entities": ["a"]}}], "votes")
        text = render_database_drdl([{"collection": "votes", "drdl_tables": votes}], "sofieSelim")
        self.assertEqual(
            text,
            "schema:\n"
            "- db: sofieSelim\n"
            "  tables:\n"
            "  - table: votes\n"
            "    collection: votes\n"
            "    pipeline: []\n"
            "    columns:\n"
            "    - Name: _id\n"
            "      MongoType: bson.ObjectId\n"
            "      SqlName: _id\n"
            "      SqlType: objectid\n"
            "    - Name: vote\n"
            "      MongoType: bool\n"
            "      SqlName: vote\n"
            "      SqlType: boolean\n"
            "    - Name: votes_filter\n"
            "      MongoType: mongo.Filter\n"
            "      SqlName: votes_filter\n"
            "      SqlType: varchar\n"
            "  - table: votes_metadata_entities\n"
            "    collection: votes\n"
            "    pipeline:\n"
            "    - $unwind:\n"
            "        includeArrayIndex: metadata.entities_idx\n"
            "        path: $metadata.entities\n"
            "        preserveNullAndEmptyArrays: false\n"
            "    columns:\n"
            "    - Name: _id\n"
            "      MongoType: bson.ObjectId\n"
            "      SqlName: _id\n"
            "      SqlType: objectid\n"
            "    - Name: metadata.entities\n"
            "      MongoType: string\n"
            "      SqlName: metadata.entities\n"
            "      SqlType: varchar\n"
            "    - Name: metadata.entities_idx\n"
            "      MongoType: int\n"
            "      SqlName: metadata.entities_idx\n"
            "      SqlType: int\n"
            "    - Name: votes_filter\n"
            "      MongoType: mongo.Filter\n"
            "      SqlName: votes_filter\n"
            "      SqlType: varchar\n",
        )

    def test_shared_stages_are_written_out_not_aliased(self):
        tables = tables_of(CONVERSATIONS, "conversations")
        text = render_database_drdl([{"collection": "conversations", "drdl_tables": tables}], "db")
        self.assertNotIn("&id", text)
        self.assertNotIn("*id", text)
        self.assertEqual(yaml.safe_load(text)["schema"][0]["tables"], tables)

    def test_plan_without_drdl_tables_is_refused(self):
        with self.assertRaises(ValueError):
            render_database_drdl([{"collection": "old"}], "db")


if __name__ == "__main__":
    unittest.main()
