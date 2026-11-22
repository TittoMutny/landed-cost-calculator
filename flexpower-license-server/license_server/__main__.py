"""Flexpower licence server command line.

    python -m license_server init [--ems ../flexpower-ems-kit]   create signing key (+ config); embed public key in the EMS
    python -m license_server hash-password                         hash for an admin in license-server.yaml
    python -m license_server run [--host 127.0.0.1 --port 8443]    start the server
    python -m license_server public-key                            print the public key
"""
from __future__ import annotations

import argparse
import getpass
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def embed_public_key(ems_dir: Path, public_key: str) -> Path:
    target = ems_dir / "ems" / "licensing.py"
    text = target.read_text(encoding="utf-8")
    new, n = re.subn(r'^FLEXPOWER_PUBLIC_KEY = ".*"$', f'FLEXPOWER_PUBLIC_KEY = "{public_key}"', text, flags=re.M)
    if n != 1:
        raise RuntimeError(f"could not find FLEXPOWER_PUBLIC_KEY in {target}")
    target.write_text(new, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="license_server")
    p.add_argument("-c", "--config", default="license-server.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--ems", help="path to the flexpower-ems-kit folder to embed the public key into")
    sub.add_parser("hash-password")
    sub.add_parser("public-key")
    r = sub.add_parser("run")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8443)
    args = p.parse_args(argv)

    from .app import ServerConfig, create_app, hash_password
    from .crypto import generate_key, load_key, public_key_b64

    if args.cmd == "hash-password":
        pw = getpass.getpass("Admin password: ")
        if len(pw) < 12:
            sys.exit("Use at least 12 characters")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("Passwords do not match")
        print(hash_password(pw))
        return

    cfg_path = Path(args.config)
    if args.cmd == "init" and not cfg_path.exists():
        shutil.copy(ROOT / "license-server.example.yaml", cfg_path)
        print(f"Created {cfg_path} - add an admin (python -m license_server hash-password).")
    cfg = ServerConfig(cfg_path)

    if args.cmd == "init":
        if cfg.key_path.exists():
            key = load_key(cfg.key_path)
            print(f"Signing key already exists: {cfg.key_path}")
        else:
            key = generate_key(cfg.key_path)
            print(f"Created signing key {cfg.key_path}. BACK IT UP and keep it secret:")
            print("  losing it means re-issuing every customer's app; leaking it lets anyone forge licences.")
        pub = public_key_b64(key)
        print(f"Public key: {pub}")
        if args.ems:
            print(f"Embedded in {embed_public_key(Path(args.ems), pub)}")
        else:
            print("Put it in flexpower-ems-kit/ems/licensing.py (FLEXPOWER_PUBLIC_KEY), or rerun with --ems.")
        return
    if args.cmd == "public-key":
        print(public_key_b64(load_key(cfg.key_path)))
        return
    if not cfg.admins:
        sys.exit("No admins in the config. Add one with a hash from: python -m license_server hash-password")
    import uvicorn

    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info", proxy_headers=True)


if __name__ == "__main__":
    main()
