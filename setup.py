#!/usr/bin/env python3
"""One-time setup: create the .env that holds your API key, then check it works.

Run this right after cloning, before the first `python -m sandbox_engine`:

    python setup.py

It prompts for an NVIDIA API key and writes it to `.env`, which is gitignored.
The key is never written to a tracked file, never printed back, and never
uploaded -- a fresh clone asks for its own, so a leaked repository is not a
leaked credential.

Pass --check on its own to verify a key you already have, and --backend ollama
if you would rather run a local model and need no key at all.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"


def read_env(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        found[name.strip()] = value.strip().strip("'\"")
    return found


def write_env(path: Path, key: str, backend: str) -> None:
    body = (
        "# Written by setup.py. This file is gitignored -- never commit it.\n"
        "# Delete it and run setup.py again to replace the key.\n"
        f"NVIDIA_API_KEY={key}\n"
        f"RAG_BACKEND={backend}\n"
    )
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def looks_like_a_key(value: str) -> bool:
    return value.startswith("nvapi-") and len(value) > 20


def check_key(key: str, model: str, base: str) -> tuple[bool, str]:
    try:
        from openai import OpenAI
    except ImportError:
        return False, "the openai package is not installed -- run: pip install -r requirements.txt"
    try:
        client = OpenAI(base_url=base, api_key=key, timeout=25, max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=8,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return False, (f"the key was refused ({status}). It authenticated but has no "
                           f"inference entitlement for {model}. Check it at build.nvidia.com.")
        return False, f"{type(exc).__name__}: {exc}"
    return True, (response.choices[0].message.content or "").strip() or "(empty reply)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="verify the key already in .env and exit")
    parser.add_argument("--backend", choices=("auto", "nvidia", "ollama"), default=None,
                        help="which backend to write (default: nvidia with a key)")
    parser.add_argument("--model", default=os.environ.get("NVIDIA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("NVIDIA_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--no-verify", action="store_true",
                        help="write the key without spending a request on it")
    args = parser.parse_args(argv)

    env = read_env(ENV_PATH)
    key = os.environ.get("NVIDIA_API_KEY", "").strip() or env.get("NVIDIA_API_KEY", "").strip()

    if args.check or args.backend == "ollama":
        if not key:
            print("No key in .env. Run `python setup.py` to add one, or")
            print("`python -m sandbox_engine.query_ui` to use a local model instead.")
            return 1
    elif not key:
        print("Get an API key at https://build.nvidia.com\n")
        try:
            key = getpass.getpass("NVIDIA API key (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if not key:
            print("\nNo key entered. Nothing was written.")
            print("You can still explore the graph:  python -m sandbox_engine.query_ui")
            return 1

    if not looks_like_a_key(key):
        print(f"That does not look like an NVIDIA key -- expected it to start with 'nvapi-'.")
        print("Nothing was written. Paste the key from https://build.nvidia.com")
        return 1

    backend = args.backend or "nvidia"
    write_env(ENV_PATH, key, backend)
    print(f"\nWrote {ENV_PATH.relative_to(ROOT)}  (gitignored, mode 600)")

    if args.backend == "ollama":
        print("Pinned to the local model. Start it with:  ollama serve")
        return 0

    if args.no_verify:
        print("Skipped the live check. Start the UI with:")
        print("    python -m sandbox_engine.query_ui")
        return 0

    print(f"Verifying against {args.model} ...")
    ok, detail = check_key(key, args.model, args.base_url)
    if ok:
        print(f"  key works -- the model replied: {detail!r}\n")
    else:
        print(f"  {detail}")
        print("\n  The key is saved, so you do not need to enter it again.")
        print("  A key with no inference entitlement for this model cannot phrase")
        print("  answers. You can still run a local model:  ollama serve")
        return 1

    print("Next:")
    print("    python -m sandbox_engine --reset        # build the graph")
    print("    python -m sandbox_engine.query_ui       # open the UI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
