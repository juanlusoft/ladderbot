#!/usr/bin/env python3
"""Actualizador OTA de LadderBot.

Solo acepta paquetes HTTPS con SHA-256 conocido, manifiesto interno y una lista
cerrada de ficheros. Conserva config, estrategias, secretos y estado.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

DEFAULT_INSTALL = Path("/opt/ladderbot")
DEFAULT_REQUEST = DEFAULT_INSTALL / "state" / "update-request.json"
MAX_DOWNLOAD = 25 * 1024 * 1024
ALLOWED_FILES = {
    "VERSION", "ladderbot.py", "telegram_cmd.py", "precio_chart.py",
    "updater.py", "ajustar_saldo.py", "leer_ingresos.py", "ver_saldos.py",
    "ver_tx.py",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError(f"versión inválida: {value!r}")
    return tuple(int(part) for part in value.split("."))


def validate_https(url: str) -> None:
    if not url.startswith("https://"):
        raise ValueError("la OTA exige una URL https://")


def download(url: str, destination: Path) -> None:
    validate_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": "LadderBot-OTA/2.1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_DOWNLOAD:
            raise ValueError("paquete OTA demasiado grande")
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ValueError("paquete OTA supera 25 MiB")
            out.write(chunk)


def safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise ValueError("paquete OTA vacío")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"ruta insegura en OTA: {member.name}")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError(f"tipo de entrada no permitido: {member.name}")
        bundle.extractall(destination, filter="data")
    if (destination / "manifest.json").is_file():
        return destination
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) == 1 and (roots[0] / "manifest.json").is_file():
        return roots[0]
    raise ValueError("manifest.json no encontrado en la raíz del paquete")


def validate_release(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version", ""))
    parse_version(version)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest.files debe ser un objeto no vacío")
    unknown = set(files) - ALLOWED_FILES
    if unknown:
        raise ValueError(f"ficheros no permitidos en OTA: {sorted(unknown)}")
    required = {"VERSION", "ladderbot.py", "telegram_cmd.py", "updater.py"}
    if not required.issubset(files):
        raise ValueError(f"faltan ficheros obligatorios: {sorted(required - set(files))}")
    for relative, expected in files.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise ValueError(f"hash inválido para {relative}")
        path = root / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"hash interno incorrecto: {relative}")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("VERSION no coincide con manifest.json")
    return manifest


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for original in path.read_text(encoding="utf-8").splitlines():
        line = original.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def telegram_notice(install: Path, text: str) -> None:
    try:
        env = load_env(install / "secrets" / "ladderbot.env")
        token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            return
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        urllib.request.urlopen(request, timeout=20).read()
    except Exception:
        pass


def no_pending_transactions(install: Path) -> None:
    state_dir = install / "state"
    for path in state_dir.glob("*.json"):
        if path.name in {"telegram_offset.json", "precio_cache.json", "update-request.json"}:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("pending"):
            raise RuntimeError(f"{path.name} tiene una transacción pendiente; OTA cancelada")


def run_checked(command: list[str], timeout: int = 180) -> None:
    subprocess.run(command, check=True, timeout=timeout)


def install_release(root: Path, manifest: dict, install: Path, restart: bool = True) -> Path:
    current_file = install / "VERSION"
    current = current_file.read_text(encoding="utf-8").strip() if current_file.exists() else "0.0.0"
    new = str(manifest["version"])
    if parse_version(new) <= parse_version(current):
        raise ValueError(f"la versión {new} no es posterior a {current}")
    no_pending_transactions(install)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = install / "backups" / f"ota-{stamp}-v{current}"
    backup.mkdir(parents=True, mode=0o700)
    absent: list[str] = []
    for relative in manifest["files"]:
        old = install / relative
        if old.exists():
            shutil.copy2(old, backup / relative)
        else:
            absent.append(relative)
    (backup / "absent.json").write_text(json.dumps(absent), encoding="utf-8")

    services = ["ladderbot.service", "ladderbot-telegram.service"]
    active_services: list[str] = []
    try:
        if restart:
            active_services = [
                service for service in services
                if subprocess.run(
                    ["systemctl", "is-active", "--quiet", service],
                    check=False, timeout=15,
                ).returncode == 0
            ]
            for service in active_services:
                subprocess.run(["systemctl", "stop", service], check=False, timeout=45)
        for relative in manifest["files"]:
            source, target = root / relative, install / relative
            temporary = target.with_name(target.name + ".ota-new")
            shutil.copy2(source, temporary)
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o700 if relative.endswith(".py") else 0o644)
            os.replace(temporary, target)
        run_checked([sys.executable, "-m", "py_compile"] + [
            str(install / name) for name in manifest["files"] if name.endswith(".py")
        ])
        if restart:
            run_checked([
                "runuser", "-u", "ladderbot", "--",
                str(install / ".venv" / "bin" / "python"),
                str(install / "ladderbot.py"), "doctor",
            ])
            for service in active_services:
                subprocess.run(["systemctl", "start", service], check=True, timeout=45)
        return backup
    except Exception:
        for relative in manifest["files"]:
            saved = backup / relative
            target = install / relative
            if saved.exists():
                shutil.copy2(saved, target)
            elif relative in absent:
                target.unlink(missing_ok=True)
        if restart:
            for service in active_services:
                subprocess.run(["systemctl", "start", service], check=False, timeout=45)
        raise


def apply_request(request_path: Path, install: Path, restart: bool = True) -> None:
    lock_path = Path("/run/ladderbot-update.lock") if install == DEFAULT_INSTALL else install / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request_path.unlink(missing_ok=True)
        url, expected = str(request["url"]), str(request["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("SHA-256 externo inválido")
        with tempfile.TemporaryDirectory(prefix="ladderbot-ota-") as temporary:
            tmp = Path(temporary)
            archive = tmp / "release.tar.gz"
            download(url, archive)
            if sha256_file(archive) != expected:
                raise ValueError("SHA-256 externo no coincide")
            root = safe_extract(archive, tmp / "extract")
            manifest = validate_release(root)
            backup = install_release(root, manifest, install, restart=restart)
        telegram_notice(install, f"✅ LadderBot actualizado por OTA a v{manifest['version']}. Backup: {backup}")


def verify_archive(archive: Path, expected: str | None) -> dict:
    if expected and sha256_file(archive) != expected.lower():
        raise ValueError("SHA-256 externo no coincide")
    with tempfile.TemporaryDirectory(prefix="ladderbot-verify-") as temporary:
        root = safe_extract(archive, Path(temporary))
        return validate_release(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p = sub.add_parser("apply-request")
    apply_p.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    apply_p.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL)
    apply_p.add_argument("--no-restart", action="store_true")
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("archive", type=Path)
    verify_p.add_argument("--sha256")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            manifest = verify_archive(args.archive, args.sha256)
            print(json.dumps({"ok": True, "version": manifest["version"]}))
        else:
            apply_request(args.request, args.install_dir, restart=not args.no_restart)
        return 0
    except Exception as exc:
        install = getattr(args, "install_dir", DEFAULT_INSTALL)
        telegram_notice(install, f"🚨 OTA de LadderBot cancelada: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
