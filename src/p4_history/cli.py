import argparse
import json
import sys
from pathlib import Path

from platformdirs import user_data_path

from .codec import fingerprint
from .errors import HistoryError
from .history import History
from .p4 import Perforce


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Git history for Perforce changelists.")
    parser.add_argument(
        "command",
        choices=["serve", "doctor", "call", "hook", "install-hooks", "uninstall-hooks"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--operation", help="Application operation for the JSON command interface")
    parser.add_argument("--arguments", default="{}", help="JSON object of operation arguments")
    parser.add_argument("--host", choices=["copilot", "claude-code"])
    parser.add_argument("--event", choices=["pre", "post", "stop"])
    parser.add_argument("--hook-executable", type=Path)
    parser.add_argument("--workspace", type=Path, required=True, help="Exact Perforce client root")
    parser.add_argument("--client", required=True, help="Explicit Perforce client name")
    parser.add_argument("--port", help="Optional Perforce server override")
    parser.add_argument("--user", help="Optional Perforce user override")
    parser.add_argument(
        "--state-dir", type=Path, help="Override the external local history directory"
    )
    args = parser.parse_args()
    try:
        root = args.workspace.resolve()
        key = fingerprint(
            {"root": str(root), "client": args.client, "port": args.port, "user": args.user}
        )[:24]
        directory = (
            args.state_dir or user_data_path("p4-history", appauthor=False) / key
        ).resolve()
        if args.command == "uninstall-hooks":
            from .hook_install import uninstall_hooks

            print(json.dumps(uninstall_hooks(root, directory, args.host)))
            return
        payload = None
        if args.command == "hook":
            from .hooks import relevant
            from .limits import MAX_METADATA

            if not args.host or not args.event:
                raise HistoryError("Hook commands require --host and --event.")
            raw = sys.stdin.buffer.read(MAX_METADATA + 1)
            if len(raw) > MAX_METADATA:
                raise HistoryError("Hook input exceeds the size limit.")
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise HistoryError("Hook input must be valid JSON.") from exc
            if not relevant(args.workspace.resolve(), payload, args.event):
                print("{}")
                return
        p4 = Perforce(args.workspace, args.client, args.port, args.user)
        history = History(p4, directory)
        if args.command == "doctor":
            print(json.dumps(history.inspect(), indent=2))
        elif args.command == "hook":
            from .hooks import run_hook

            print(json.dumps(run_hook(history, payload, args.event)))
        elif args.command == "install-hooks":
            from .hook_install import install_hooks

            if not args.host or not args.hook_executable:
                raise HistoryError("Hook installation requires --host and --hook-executable.")
            connection = [
                "--workspace",
                str(p4.root),
                "--client",
                args.client,
                "--state-dir",
                str(directory),
            ]
            for flag, value in (("--port", args.port), ("--user", args.user)):
                if value:
                    connection += [flag, value]
            print(
                json.dumps(
                    install_hooks(history, args.host, [str(args.hook_executable)], connection)
                )
            )
        elif args.command == "call":
            operations = {
                "inspect",
                "begin",
                "commit",
                "prepare",
                "create_change",
                "status",
                "log",
                "diff",
                "plan_restore",
                "apply_restore",
                "recover_restore",
                "label",
                "labels",
                "branch",
                "branches",
                "plan_switch",
                "plan_merge",
                "continue_task",
                "show",
                "storage",
                "automatic_checkpoints",
            }
            if args.operation not in operations:
                raise HistoryError("Choose a supported application operation with --operation.")
            try:
                arguments = json.loads(args.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError
            except ValueError as exc:
                raise HistoryError("--arguments must be a JSON object.") from exc
            from pydantic import ValidationError, validate_call

            operation = validate_call(getattr(history, args.operation), config={"strict": True})
            try:
                result = operation(**arguments)
            except ValidationError as exc:
                details = "; ".join(e["msg"] for e in exc.errors(include_input=False)[:3])
                raise HistoryError(f"Invalid operation arguments: {details}") from exc
            print(json.dumps(result, indent=2))
        else:
            from .server import create_server

            create_server(history).run(transport="stdio")
    except (HistoryError, OSError) as exc:
        print(f"p4-history: {exc}", file=sys.stderr)
        if args.command == "hook" and args.event == "pre":
            from .hooks import denial

            print(json.dumps(denial(args.host, str(exc))))
            raise SystemExit(2) from exc
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
