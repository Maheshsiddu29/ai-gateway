"""Schema administration for the agent runtime, run with the schema owner's credentials.

    python -m agent_runtime.persistence status
    python -m agent_runtime.persistence upgrade [--grant-to ROLE]
    python -m agent_runtime.persistence downgrade --to VERSION [--allow-data-loss]

The owner connection comes from AGENT_DATABASE_OWNER_URL and the schema from
AGENT_DATABASE_SCHEMA. The API and worker never run these commands.
"""

import argparse
import asyncio
import json
import sys

from agent_runtime.config import MigrationSettings
from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import connect


async def _run(args: argparse.Namespace, settings: MigrationSettings) -> dict[str, object]:
    schema = settings.database_schema
    conn = await connect(settings.database_owner_url, schema, "agent-runtime-migrations")
    async with conn:
        if args.command == "upgrade":
            applied = await migrations.upgrade(conn, schema)
            role = args.grant_to or settings.database_app_role
            if role is not None:
                await migrations.grant(conn, schema, role)
            return {"applied": applied, "granted_to": role}
        if args.command == "downgrade":
            reverted = await migrations.downgrade(
                conn, schema, target=args.to, allow_data_loss=args.allow_data_loss
            )
            return {"reverted": reverted}
        versions = sorted(await migrations.applied_versions(conn))
        return {"applied": versions, "latest": migrations.LATEST_VERSION}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agent_runtime.persistence")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show applied migrations")
    upgrade = commands.add_parser("upgrade", help="apply all pending migrations")
    upgrade.add_argument("--grant-to", help="role to receive the application privileges")
    downgrade = commands.add_parser("downgrade", help="revert migrations above a version")
    downgrade.add_argument("--to", type=int, required=True)
    downgrade.add_argument("--allow-data-loss", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args, MigrationSettings()))
    except migrations.SchemaError as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
