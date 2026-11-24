"""Exercise routing and verified project setup against an in-memory Meko contract."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import live
import meko
import verify_meko


class FakeMeko:
    def __init__(self):
        self.project = None
        self.folders = []
        self.datapacks = []
        self.calls = []
        self.conversations = {}
        self.memories = {}

    async def call_tool(self, name, args):
        self.calls.append((name, args.copy()))
        pack = args.get("datapack_id")
        if name == "datapack_describe":
            result = {"datapack_id": pack, "datapack_name": pack, "project": None}
        elif name == "project_list":
            result = {"owned": [self.project] if self.project else [], "shared": []}
        elif name == "project_create":
            self.project = {"project_id": "p", "project_name": args["project_name"]}
            for folder in args["folders"]:
                self.folders.append({"folder_name": folder, "folder_id": folder})
            result = self.project
        elif name == "project_describe":
            result = {"project": self.project, "folders": self.folders, "datapacks": self.datapacks}
        elif name == "project_add_datapack":
            self.datapacks.append({"datapack_id": pack, "folder_id": args["folder_id"]})
            result = {"datapack_id": pack}
        elif name == "conversation_create":
            cid = str(len(self.conversations))
            self.conversations[cid] = (pack, [])
            result = {"conversation_id": cid}
        else:
            target, messages = self.conversations[args["conversation_id"]]
            if target != pack:
                raise AssertionError("Conversation belongs to another datapack")
            if name == "memory_add":
                self.memories.setdefault(pack, []).append({"memory": args["text"], "metadata": args["metadata"]})
                result = {"success": True}
            elif name == "context_search":
                result = {"memory": {"status": "ok", "results": self.memories.get(pack, [])}}
            elif name == "memory_search":
                result = {"results": self.memories.get(pack, [])}
            elif name == "knowledgebase_search":
                result = {"results": []}
            elif name == "conversation_add_message":
                messages.append({"input": args["input"], "output": args["output"]})
                result = {"success": True}
            elif name == "conversation_get":
                result = {"messages": messages}
            else:
                raise AssertionError(name)
        return SimpleNamespace(isError=False, structuredContent={"data": result})


class LiveTests(unittest.TestCase):
    def test_link_twice_and_run_two_pack_acceptance(self):
        session = FakeMeko()

        @asynccontextmanager
        async def connection():
            yield session

        with patch.object(meko, "connection", connection), patch.dict(os.environ, {
                "MEKO_DATAPACK_ID": "ops", "MEKO_RAW_DATAPACK_ID": "raw", "MEKO_API_KEY": "fixture"}):
            for _ in range(2):
                asyncio.run(live.connect({"ops": "Operations", "raw": "Alert intake"}))
            verify_meko.main()
        names = [name for name, _ in session.calls]
        self.assertEqual(names.count("project_create"), 1)
        self.assertEqual(names.count("project_add_datapack"), 2)
        self.assertEqual(set(session.memories), {"ops"})
        self.assertEqual(sum(name == "conversation_add_message" and a["datapack_id"] == "raw" for name, a in session.calls), 1)
        self.assertTrue(all("datapack_id" not in a for name, a in session.calls
                            if name in {"project_create", "project_list", "project_describe"}))

    def test_foreign_placement_and_shared_name_protect_existing_data(self):
        from unittest.mock import AsyncMock
        pack = {"datapack_id": "ops", "name": "Scarline", "project": "Elsewhere"}
        target = {"ops": "Operations"}
        with patch.object(meko, "call", AsyncMock(return_value=pack)) as calls:
            with self.assertRaisesRegex(live.SetupError, "another project"):
                asyncio.run(live.link_packs(None, target))
            self.assertEqual(calls.await_count, 1)
        pack["project"] = None
        listing = {"owned": [], "shared": [{"project_name": "Scarline", "project_id": "shared"}]}
        with patch.object(meko, "call", AsyncMock(side_effect=[pack, listing])) as calls:
            with self.assertRaisesRegex(live.SetupError, "shared"):
                asyncio.run(live.link_packs(None, target))
            self.assertEqual(calls.await_count, 2)

    def test_changed_configuration_keeps_queued_writes_on_original_pack(self):
        from store import initialize, transaction
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "SCARLINE_DB": str(Path(tmp)/"db"), "DATABASE_URL": "", "MEKO_API_KEY": "fixture",
                "MEKO_DATAPACK_ID": "before", "MEKO_RAW_DATAPACK_ID": ""}):
            initialize()
            with transaction(True) as q:
                meko.enqueue(q, "trace", "id", "memory_add", {"text": "fixture"})
            with patch.dict(os.environ, {"MEKO_DATAPACK_ID": "after"}):
                self.assertFalse(meko.deliver_one())
            with transaction() as q:
                row = q("SELECT state,datapack_id FROM cloud_jobs").fetchone()
                self.assertEqual((row["state"], row["datapack_id"]), ("queued", "before"))
