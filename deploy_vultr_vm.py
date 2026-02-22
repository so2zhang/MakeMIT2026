#!/usr/bin/env python3
"""Deploy Vultr backend to a Vultr VM via SSH.

Required env vars:
  VM_HOST, VM_USER, VM_PASSWORD
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SSLMODE
"""

from pathlib import Path
import os

import paramiko


APP_DIR = "/opt/makemit-vultr"

FILES_TO_UPLOAD = [
    "vultr_backend.py",
    "vultr_requirements.txt",
    "vultr_schema.sql",
    "vultr_templates/index.html",
]


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value or ""


def _load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def run(ssh: paramiko.SSHClient, cmd: str) -> None:
    _, stdout, stderr = ssh.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    if out.strip():
        print(out.strip())
    if code != 0:
        raise RuntimeError(f"Command failed ({code}): {cmd}\n{err}")


def main():
    _load_dotenv()
    vm_host = _env("VM_HOST", required=True)
    vm_user = _env("VM_USER", "root")
    vm_password = _env("VM_PASSWORD", required=True)

    db_env = {
        "DB_HOST": _env("DB_HOST", required=True),
        "DB_PORT": _env("DB_PORT", "5432"),
        "DB_NAME": _env("DB_NAME", "defaultdb"),
        "DB_USER": _env("DB_USER", required=True),
        "DB_PASSWORD": _env("DB_PASSWORD", required=True),
        "DB_SSLMODE": _env("DB_SSLMODE", "require"),
    }

    root = Path(__file__).resolve().parent
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(vm_host, username=vm_user, password=vm_password, timeout=20)

    run(ssh, "apt-get update -y && apt-get install -y python3-venv python3-pip")
    run(ssh, f"mkdir -p {APP_DIR}/vultr_templates")

    sftp = ssh.open_sftp()
    try:
        for rel in FILES_TO_UPLOAD:
            src = root / rel
            dst = f"{APP_DIR}/{rel}"
            sftp.put(str(src), dst)
        env_path = f"{APP_DIR}/.env"
        with sftp.file(env_path, "w") as f:
            for k, v in db_env.items():
                f.write(f"{k}={v}\n")
    finally:
        sftp.close()

    run(
        ssh,
        f"python3 -m venv {APP_DIR}/.venv && "
        f"{APP_DIR}/.venv/bin/pip install --upgrade pip && "
        f"{APP_DIR}/.venv/bin/pip install -r {APP_DIR}/vultr_requirements.txt",
    )

    service = f"""[Unit]
Description=MakeMIT Vultr Backend
After=network.target

[Service]
Type=simple
WorkingDirectory={APP_DIR}
EnvironmentFile={APP_DIR}/.env
ExecStart={APP_DIR}/.venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 vultr_backend:app
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""

    run(
        ssh,
        "cat > /etc/systemd/system/makemit-vultr.service <<'EOF'\n" + service + "\nEOF",
    )
    run(ssh, "systemctl daemon-reload && systemctl enable makemit-vultr && systemctl restart makemit-vultr")
    run(ssh, "systemctl --no-pager --full status makemit-vultr | head -n 20")
    run(ssh, "ufw allow 8000/tcp || true")
    print(f"Deployment complete. Open: http://{vm_host}:8000")
    ssh.close()


if __name__ == "__main__":
    main()
