from __future__ import annotations

import sys

from .cli import main as run_cli


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _run_app()
        return

    if argv[0] in {"cli", "run"}:
        raise SystemExit(run_cli(argv[1:]))
    if argv[0] == "--cli":
        raise SystemExit(run_cli(argv[1:]))
    if argv[0] == "gui":
        _run_app()
        return
    if argv[0] in {"-h", "--help"}:
        _print_help()
        return

    print(f"Unknown argument: {argv[0]}", file=sys.stderr)
    _print_help()
    raise SystemExit(2)


def _print_help() -> None:
    print(
        "RE File Hash Exporter\n"
        "\n"
        "Usage:\n"
        "  python main.py                 Start the GUI\n"
        "  python main.py gui             Start the GUI\n"
        "  python main.py --cli <config.toml>  Run CLI mode from a TOML config file\n"
        "  python main.py cli <config.toml>    Run CLI mode from a TOML config file\n"
        "\n"
        "Relative paths inside the config are resolved from the config file's directory.",
    )


def _run_app() -> None:
    from .ui.app import run_app

    run_app()


if __name__ == "__main__":
    main()
