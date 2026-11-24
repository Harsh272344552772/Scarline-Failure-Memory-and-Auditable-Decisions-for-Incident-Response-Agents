"""File the existing datapacks, verify the operations datapack, then serve Scarline."""
import argparse
import asyncio
import getpass
import os
import sys
import warnings
import re
from pathlib import Path
from uuid import UUID

import meko
import verify_meko


class SetupError(Exception):
    pass


def project_matches(rows):
    return [row for row in rows if row.get("project_name", "").casefold() == "scarline"]


async def project_tree(session, project_id):
    data = await meko.call(session, "project_describe", {"project_id": project_id})
    if (data.get("project", {}).get("project_id") != project_id or
            not isinstance(data.get("folders"), list) or not isinstance(data.get("datapacks"), list)):
        raise SetupError("Meko did not return a verifiable project tree.")
    return data


async def existing_or_new_project(session, folders):
    listing = await meko.call(session, "project_list", {})
    if not isinstance(listing.get("owned"), list) or not isinstance(listing.get("shared"), list):
        raise SetupError("Meko did not return its owned and shared project lists.")
    owned = project_matches(listing["owned"])
    if len(owned) == 1:
        return owned[0]["project_id"]
    if owned or project_matches(listing["shared"]):
        raise SetupError("Scarline is ambiguous or shared. Check Projects in the owner account.")
    created = await meko.call(session, "project_create", {
        "project_name": "Scarline", "description": "Failure memory for incident response.", "folders": folders
    })
    return created["project_id"]


async def link_packs(session, targets):
    descriptions = {}
    for pack in targets:
        reply = await meko.call(session, "datapack_describe", {}, pack)
        desc = reply.get("datapack", reply)
        name = desc.get("datapack_name") or desc.get("name")
        if (desc.get("datapack_id", desc.get("id")) != pack or not isinstance(name, str)
                or not name or name.casefold() == "meko_default_datapack"):
            raise SetupError("The selected datapack could not be identified as a dedicated Scarline target.")
        if desc.get("project") and str(desc["project"]).casefold() != "scarline":
            raise SetupError(f"{name} already belongs to another project. Review its placement in Meko.")
        descriptions[pack] = (name, desc)
    project_id = await existing_or_new_project(session, list(targets.values()))
    for pack, folder_name in targets.items():
        name, desc = descriptions[pack]
        tree = await project_tree(session, project_id)
        filed = next((row for row in tree["datapacks"] if row["datapack_id"] == pack), None)
        if not filed:
            if "project" not in desc or desc["project"]:
                raise SetupError(f"Meko could not establish that {name} is unfiled. Check its project in the portal.")
            if desc.get("grant") and desc["grant"] != "owner":
                raise SetupError(f"Only the owner can file {name}.")
            folder = next((f for f in tree["folders"] if f["folder_name"].casefold() == folder_name.casefold()), None)
            if not folder:
                folder = await meko.call(session, "project_folder_create", {
                    "project_id": project_id, "folder_name": folder_name
                })
            await meko.call(session, "project_add_datapack", {"folder_id": folder["folder_id"], "datapack_id": pack})
            tree = await project_tree(session, project_id)
            filed = next((row for row in tree["datapacks"] if row["datapack_id"] == pack
                          and row["folder_id"] == folder["folder_id"]), None)
        folder = next((f for f in tree["folders"] if filed and f["folder_id"] == filed["folder_id"]), None)
        if not folder:
            raise SetupError(f"Meko did not confirm the placement of {name}. Inspect the project before retrying.")
        print(f"Verified project membership: Scarline / {folder['folder_name']} / {name} ({pack})")


async def connect(targets):
    async with asyncio.timeout(60), meko.connection() as session:
        await link_packs(session, targets)


def configure(key_file=None):
    ops = str(UUID(os.getenv("MEKO_DATAPACK_ID") or meko.SCARLINE_PACK))
    targets = {ops: "Operations"}
    os.environ["MEKO_DATAPACK_ID"] = ops
    if raw := os.getenv("MEKO_RAW_DATAPACK_ID"):
        raw = str(UUID(raw))
        if raw == ops:
            raise SetupError("Use different operational and raw datapack IDs.")
        os.environ["MEKO_RAW_DATAPACK_ID"] = raw
        targets[raw] = "Alert intake"
    for variable, folder in (("MEKO_KNOWLEDGE_DATAPACK_ID", "Knowledge"), ("MEKO_AUDIT_DATAPACK_ID", "Audit")):
        if value := os.getenv(variable):
            value = str(UUID(value))
            os.environ[variable] = value
            targets.setdefault(value, folder)
    if key_file:
        matches = re.findall(r"mko_tkn_[A-Za-z0-9_-]+", Path(key_file).read_text())
        if len(matches) != 1:
            raise SetupError("The key file must contain exactly one Meko API key.")
        os.environ["MEKO_API_KEY"] = matches[0]
    if not os.getenv("MEKO_API_KEY"):
        if not sys.stdin.isatty():
            raise SetupError("Set MEKO_API_KEY in the host's secret settings or run in an interactive terminal.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Meko API key (hidden): ").strip()
        if not key:
            raise SetupError("A Meko key is required for live mode.")
        os.environ["MEKO_API_KEY"] = key
    return targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Verify and exit before serving.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--key-file", help="Read a local key file without copying it into the project.")
    parser.add_argument("--with-runbook", action="store_true", help="Upload and verify the bundled knowledge document.")
    args = parser.parse_args()
    try:
        targets = configure(args.key_file)
        asyncio.run(connect(targets))
        verify_meko.main()
        if args.with_runbook:
            from store import initialize
            import cloud_actions
            initialize()
            result = cloud_actions.perform("runbook")
            print("Verified runbook:", result["filename"], result["bytes"], "bytes")
    except SetupError as exc:
        sys.exit(str(exc))
    except (ValueError, KeyError, getpass.GetPassWarning):
        sys.exit("Check the datapack UUIDs, terminal and Meko project response. Setup did not finish.")
    except Exception:
        sys.exit("Meko setup did not finish. Check the network, key access and project activity before retrying.")
    if not args.verify_only:
        import uvicorn
        print(f"Open http://127.0.0.1:{args.port} for the live incident feed.")
        uvicorn.run("app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
