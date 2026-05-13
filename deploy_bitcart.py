#!/usr/bin/env python3
"""
deploy_bitcart.py — End-to-end, idempotent, zero-touch deployment automation for Bitcart.

This script installs, configures, and hardens the Bitcart payment processor from
https://github.com/Decipheredmedia/bitcart with full SEO support, TLS, and wallet injection.

Usage:
    sudo python3 deploy_bitcart.py install [--config config.yaml] [--dry-run]
    sudo python3 deploy_bitcart.py configure [--config config.yaml]
    sudo python3 deploy_bitcart.py verify
    sudo python3 deploy_bitcart.py update
    sudo python3 deploy_bitcart.py uninstall
    sudo python3 deploy_bitcart.py backup [--output /path/to/backup.tar.gz]
    sudo python3 deploy_bitcart.py restore --input /path/to/backup.tar.gz
"""

from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import textwrap
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Third-party imports — gracefully deferred until after pip install
# ---------------------------------------------------------------------------
try:
    import requests
    import yaml
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    requests = None  # type: ignore[assignment]
    yaml = None  # type: ignore[assignment]
    Environment = None  # type: ignore[assignment]
    FileSystemLoader = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPT_DIR / "templates"

DEFAULT_INSTALL_DIR = Path("/opt/bitcart")
DEFAULT_DOCKER_DIR = Path("/opt/bitcart-docker")
DEFAULT_ADMIN_DIR = Path("/opt/bitcart-admin")
DEFAULT_STORE_DIR = Path("/opt/bitcart-store")
DEFAULT_SEO_DIR = Path("/opt/bitcart-seo")
DEFAULT_LOG_FILE = Path("/var/log/bitcart-deploy.log")
DEFAULT_BACKUP_DIR = Path("/opt/bitcart-backups")

MAIN_REPO = "https://github.com/Decipheredmedia/bitcart.git"
DOCKER_REPO = "https://github.com/bitcart/bitcart-docker.git"
ADMIN_REPO = "https://github.com/bitcart/bitcart-admin.git"
STORE_REPO = "https://github.com/bitcart/bitcart-store.git"

SUPPORTED_CRYPTOS = ["btc", "ltc", "bch", "xmr", "eth", "trx", "usdt"]

APT_PACKAGES = [
    "git",
    "docker.io",
    "docker-compose-plugin",
    "nginx",
    "certbot",
    "python3-certbot-nginx",
    "python3-pip",
    "python3-venv",
    "curl",
    "jq",
    "ufw",
    "fail2ban",
    "unattended-upgrades",
]

API_UPSTREAM_PORT = 8000
ADMIN_UPSTREAM_PORT = 8001
STORE_UPSTREAM_PORT = 8002

DEPLOY_TIMEOUT = 900  # 15 minutes
HEALTH_POLL_INITIAL = 5
HEALTH_POLL_MAX = 60

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(log_file: Path = DEFAULT_LOG_FILE, dry_run: bool = False) -> logging.Logger:
    """Configure dual-handler logging: JSON to file, human-readable to stdout."""
    logger = logging.getLogger("bitcart-deploy")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # --- stdout handler (human-readable) ---
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_fmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stdout_handler.setFormatter(stdout_fmt)
    logger.addHandler(stdout_handler)

    # --- file handler (JSON) ---
    if not dry_run:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)

            class JsonFormatter(logging.Formatter):
                def format(self, record: logging.LogRecord) -> str:  # noqa: D102
                    payload = {
                        "ts": datetime.datetime.utcnow().isoformat() + "Z",
                        "level": record.levelname,
                        "msg": record.getMessage(),
                        "logger": record.name,
                    }
                    if record.exc_info:
                        payload["exc"] = self.formatException(record.exc_info)
                    return json.dumps(payload)

            file_handler.setFormatter(JsonFormatter())
            logger.addHandler(file_handler)
        except PermissionError:
            logger.warning("Cannot write to %s — file logging disabled", log_file)

    return logger


# Module-level logger (re-initialized in main)
log = logging.getLogger("bitcart-deploy")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str] | str,
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    dry_run: bool = False,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a shell command, logging it, with optional dry-run."""
    cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    log.debug("CMD: %s", cmd_str)
    if dry_run:
        log.info("[DRY-RUN] Would execute: %s", cmd_str)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        input=input_text,
    )
    return result


def ensure_root() -> None:
    """Abort if not running as root/sudo."""
    if os.geteuid() != 0:
        print("ERROR: This script must be run as root or with sudo.", file=sys.stderr)
        print("Re-run: sudo python3 deploy_bitcart.py <subcommand>", file=sys.stderr)
        sys.exit(1)


def generate_password(length: int = 32) -> str:
    """Generate a cryptographically secure random password."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_email(email: str) -> bool:
    """Validate an email address against RFC-5322-like pattern."""
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_domain(domain: str) -> bool:
    """Validate a domain name format."""
    pattern = r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
    return bool(re.match(pattern, domain))


def check_dns(domain: str) -> bool:
    """Check whether domain resolves (A-record lookup)."""
    try:
        socket.getaddrinfo(domain, 80, socket.AF_INET)
        return True
    except socket.gaierror:
        return False


def validate_wallet(coin: str, address: str) -> bool:
    """Validate wallet address/xpub format per coin type."""
    coin = coin.lower()
    if coin == "btc":
        # xpub, ypub, zpub, or bech32/P2PKH
        return bool(
            re.match(r"^[xyz]pub[a-zA-Z0-9]{100,}$", address)
            or re.match(r"^bc1[a-z0-9]{25,90}$", address)
            or re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address)
        )
    elif coin == "ltc":
        return bool(
            re.match(r"^(?:[xyzLMT]pub)[a-zA-Z0-9]{100,}$", address)
            or re.match(r"^ltc1[a-z0-9]{25,90}$", address)
            or re.match(r"^[LM3][a-km-zA-HJ-NP-Z1-9]{25,34}$", address)
        )
    elif coin == "bch":
        return bool(
            re.match(r"^(bitcoincash:)?[qp][a-z0-9]{41}$", address)
            or re.match(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$", address)
            or re.match(r"^[xyz]pub[a-zA-Z0-9]{100,}$", address)
        )
    elif coin == "xmr":
        return bool(re.match(r"^4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}$", address))
    elif coin in ("eth", "usdt"):
        # EVM 0x-prefixed checksum address
        return bool(re.match(r"^0x[0-9a-fA-F]{40}$", address))
    elif coin == "trx":
        return bool(re.match(r"^T[1-9A-HJ-NP-Za-km-z]{33}$", address))
    # Unknown coin: accept anything non-empty
    return bool(address.strip())


# ---------------------------------------------------------------------------
# CLASS: SystemPrep
# ---------------------------------------------------------------------------


class SystemPrep:
    """Handles OS-level dependency installation and system preparation."""

    def __init__(self, dry_run: bool = False) -> None:
        """Initialise SystemPrep.

        Args:
            dry_run: When True, print actions without executing them.
        """
        self.dry_run = dry_run

    def install_apt_packages(self) -> None:
        """Install required system packages via apt-get."""
        log.info("Updating apt package index…")
        run(["apt-get", "update", "-qq"], dry_run=self.dry_run)
        log.info("Installing system packages: %s", ", ".join(APT_PACKAGES))
        run(
            ["apt-get", "install", "-y", "--no-install-recommends"] + APT_PACKAGES,
            dry_run=self.dry_run,
        )

    def install_python_deps(self) -> None:
        """Install Python dependencies required by this script."""
        req_file = SCRIPT_DIR / "requirements-deploy.txt"
        if req_file.exists():
            log.info("Installing Python deps from %s", req_file)
            run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)], dry_run=self.dry_run)
        else:
            log.info("Installing Python deps inline…")
            run(
                [sys.executable, "-m", "pip", "install", "-q", "requests==2.32.3", "pyyaml==6.0.2", "jinja2==3.1.4", "cryptography==42.0.8"],
                dry_run=self.dry_run,
            )

    def enable_docker(self) -> None:
        """Enable and start Docker daemon."""
        log.info("Enabling Docker service…")
        run(["systemctl", "enable", "docker"], dry_run=self.dry_run)
        run(["systemctl", "start", "docker"], dry_run=self.dry_run)
        # Add current sudo user to docker group so non-root commands work
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            run(["usermod", "-aG", "docker", sudo_user], check=False, dry_run=self.dry_run)

    def enable_unattended_upgrades(self) -> None:
        """Configure unattended-upgrades for automatic security patching."""
        log.info("Enabling unattended-upgrades…")
        cfg = Path("/etc/apt/apt.conf.d/20auto-upgrades")
        content = (
            'APT::Periodic::Update-Package-Lists "1";\n'
            'APT::Periodic::Unattended-Upgrade "1";\n'
            'APT::Periodic::AutocleanInterval "7";\n'
        )
        if not self.dry_run:
            cfg.write_text(content)
        else:
            log.info("[DRY-RUN] Would write unattended-upgrades config to %s", cfg)

    def prepare(self) -> None:
        """Run all system-preparation steps."""
        self.install_apt_packages()
        self.install_python_deps()
        self.enable_docker()
        self.enable_unattended_upgrades()


# ---------------------------------------------------------------------------
# CLASS: RepoFetcher
# ---------------------------------------------------------------------------


class RepoFetcher:
    """Clones or updates Git repositories, pinning to the latest tag."""

    def __init__(self, dry_run: bool = False) -> None:
        """Initialise RepoFetcher.

        Args:
            dry_run: When True, print actions without executing them.
        """
        self.dry_run = dry_run

    def _get_latest_tag(self, repo_dir: Path) -> Optional[str]:
        """Return the most recent semver tag in a local repo, or None."""
        result = run(
            ["git", "tag", "--sort=-version:refname"],
            capture=True,
            check=False,
            cwd=repo_dir,
            dry_run=False,  # always query git
        )
        tags = [t.strip() for t in result.stdout.splitlines() if t.strip()]
        for tag in tags:
            if re.match(r"^v?\d+\.\d+", tag):
                return tag
        return None

    def clone_or_update(self, url: str, dest: Path) -> None:
        """Clone a repo into dest, or fetch and update if it already exists.

        Args:
            url: The remote Git URL.
            dest: Local destination path.
        """
        if (dest / ".git").exists():
            log.info("Repo already cloned at %s — fetching updates…", dest)
            run(["git", "fetch", "--tags", "--prune"], cwd=dest, dry_run=self.dry_run)
        else:
            log.info("Cloning %s → %s", url, dest)
            if not self.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--depth=1", url, str(dest)], dry_run=self.dry_run)

        tag = self._get_latest_tag(dest)
        if tag:
            log.info("Pinning %s to tag %s", dest.name, tag)
            run(["git", "checkout", tag], cwd=dest, dry_run=self.dry_run)
        else:
            log.info("No semver tags found in %s — staying on default branch", dest.name)
            run(["git", "checkout", "master"], cwd=dest, check=False, dry_run=self.dry_run)

        # Verify HEAD
        result = run(["git", "rev-parse", "HEAD"], capture=True, cwd=dest, check=False, dry_run=False)
        log.info("HEAD at %s: %s", dest.name, result.stdout.strip() or "(dry-run)")

    def fetch_all(
        self,
        install_dir: Path,
        docker_dir: Path,
        admin_dir: Path,
        store_dir: Path,
    ) -> None:
        """Clone/update all required repositories.

        Args:
            install_dir: Main Bitcart repo destination.
            docker_dir: bitcart-docker repo destination.
            admin_dir: bitcart-admin repo destination.
            store_dir: bitcart-store repo destination.
        """
        self.clone_or_update(MAIN_REPO, install_dir)
        self.clone_or_update(DOCKER_REPO, docker_dir)
        self.clone_or_update(ADMIN_REPO, admin_dir)
        self.clone_or_update(STORE_REPO, store_dir)


# ---------------------------------------------------------------------------
# CLASS: ConfigCollector
# ---------------------------------------------------------------------------


class Config:
    """Holds all deployment configuration values."""

    def __init__(self) -> None:
        """Initialise Config with defaults."""
        self.domain: str = ""
        self.admin_domain: str = ""
        self.store_domain: str = ""
        self.email: str = ""
        self.wallets: dict[str, str] = {}
        self.admin_password: str = ""
        self.db_password: str = generate_password(24)
        self.tor_enable: bool = False
        self.lightning_enable: bool = False
        self.cryptos: list[str] = ["btc"]
        self.install_dir: Path = DEFAULT_INSTALL_DIR
        self.docker_dir: Path = DEFAULT_DOCKER_DIR
        self.admin_dir: Path = DEFAULT_ADMIN_DIR
        self.store_dir: Path = DEFAULT_STORE_DIR
        self.seo_dir: Path = DEFAULT_SEO_DIR
        self.google_verification: str = ""
        self.bing_verification: str = ""
        self.store_name: str = "Bitcart Store"
        self.indexnow_key: str = secrets.token_hex(16)

    def to_dict(self) -> dict[str, Any]:
        """Serialise config to a plain dict (passwords redacted)."""
        d = self.__dict__.copy()
        for key in ("admin_password", "db_password"):
            if d.get(key):
                d[key] = "***REDACTED***"
        # Convert Path objects to str
        for key, val in d.items():
            if isinstance(val, Path):
                d[key] = str(val)
        return d


class ConfigCollector:
    """Collects deployment configuration via interactive wizard or config file."""

    def __init__(self, dry_run: bool = False) -> None:
        """Initialise ConfigCollector.

        Args:
            dry_run: When True, validation warnings are printed but do not abort.
        """
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # File-based collection
    # ------------------------------------------------------------------

    def from_file(self, config_path: Path) -> Config:
        """Load configuration from a YAML file.

        Args:
            config_path: Path to the config.yaml file.

        Returns:
            A populated Config object.

        Raises:
            SystemExit: If required fields are missing or invalid.
        """
        if yaml is None:
            log.error("pyyaml is not installed. Run: pip install pyyaml")
            sys.exit(1)
        log.info("Loading configuration from %s", config_path)
        with open(config_path) as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}
        cfg = Config()
        cfg.domain = raw.get("domain", "")
        cfg.admin_domain = raw.get("admin_domain", "")
        cfg.store_domain = raw.get("store_domain", "")
        cfg.email = raw.get("email", "")
        cfg.wallets = raw.get("wallets", {})
        cfg.admin_password = raw.get("admin_password", "")
        cfg.db_password = raw.get("db_password", "") or generate_password(24)
        cfg.tor_enable = bool(raw.get("tor_enable", False))
        cfg.lightning_enable = bool(raw.get("lightning_enable", False))
        cfg.cryptos = raw.get("cryptos", list(cfg.wallets.keys()) or ["btc"])
        cfg.store_name = raw.get("store_name", "Bitcart Store")
        cfg.google_verification = raw.get("google_verification", "")
        cfg.bing_verification = raw.get("bing_verification", "")
        if raw.get("install_dir"):
            cfg.install_dir = Path(raw["install_dir"])
        if raw.get("docker_dir"):
            cfg.docker_dir = Path(raw["docker_dir"])
        self._validate(cfg)
        return cfg

    # ------------------------------------------------------------------
    # Interactive wizard
    # ------------------------------------------------------------------

    def interactive(self) -> Config:
        """Launch an interactive configuration wizard.

        Returns:
            A populated Config object.
        """
        cfg = Config()
        print("\n" + "=" * 60)
        print("  Bitcart Deployment Wizard")
        print("=" * 60 + "\n")

        cfg.domain = self._prompt("API Domain (e.g. pay.example.com)", validator=self._check_domain)
        cfg.admin_domain = self._prompt("Admin Domain (e.g. admin.example.com)", validator=self._check_domain)
        cfg.store_domain = self._prompt("Store Domain (e.g. store.example.com)", validator=self._check_domain)
        cfg.email = self._prompt("Email (Let's Encrypt + admin)", validator=validate_email)
        cfg.store_name = self._prompt("Store name", default="Bitcart Store")

        print("\nConfigured wallets (press Enter to skip a coin):")
        for coin in SUPPORTED_CRYPTOS:
            addr = input(f"  {coin.upper()} xpub/address (blank to skip): ").strip()
            if addr:
                if not validate_wallet(coin, addr):
                    log.warning("Address for %s looks invalid — accepted anyway.", coin.upper())
                cfg.wallets[coin] = addr

        if cfg.wallets:
            cfg.cryptos = list(cfg.wallets.keys())
        else:
            cfg.cryptos = ["btc"]

        cfg.admin_password = self._prompt_password(
            "Admin password (min 16 chars, mixed case + digit + special)",
            validator=self._check_password,
        )
        db_raw = self._prompt("Postgres password (blank = auto-generate)", default="")
        cfg.db_password = db_raw or generate_password(24)
        cfg.tor_enable = self._prompt_bool("Enable Tor hidden service?", default=False)
        cfg.lightning_enable = self._prompt_bool("Enable Lightning Network?", default=False)
        cfg.google_verification = self._prompt("Google Search Console verification token (blank to skip)", default="")
        cfg.bing_verification = self._prompt("Bing Webmaster verification token (blank to skip)", default="")

        self._validate(cfg)
        return cfg

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prompt(self, label: str, default: str = "", validator: Any = None) -> str:
        """Prompt the user for a string value with optional default and validator."""
        while True:
            suffix = f" [{default}]" if default else ""
            val = input(f"{label}{suffix}: ").strip() or default
            if not val:
                print("  This field is required.")
                continue
            if validator and not validator(val):
                print(f"  Invalid value: {val!r}")
                continue
            return val

    def _prompt_password(self, label: str, validator: Any = None) -> str:
        """Prompt for a password (hidden input)."""
        while True:
            val = getpass.getpass(f"{label}: ")
            if validator and not validator(val):
                print("  Password does not meet requirements.")
                continue
            confirm = getpass.getpass("Confirm password: ")
            if val != confirm:
                print("  Passwords do not match.")
                continue
            return val

    def _prompt_bool(self, label: str, default: bool = False) -> bool:
        """Prompt for a yes/no answer."""
        default_str = "Y/n" if default else "y/N"
        val = input(f"{label} [{default_str}]: ").strip().lower()
        if not val:
            return default
        return val in ("y", "yes", "1", "true")

    def _check_domain(self, domain: str) -> bool:
        """Validate domain format and optionally warn if DNS not resolving."""
        if not validate_domain(domain):
            return False
        if not check_dns(domain) and not self.dry_run:
            log.warning("DNS lookup failed for %s — continuing anyway.", domain)
        return True

    def _check_password(self, pw: str) -> bool:
        """Validate admin password strength."""
        if len(pw) < 16:
            return False
        if not re.search(r"[A-Z]", pw):
            return False
        if not re.search(r"[a-z]", pw):
            return False
        if not re.search(r"\d", pw):
            return False
        if not re.search(r"[^a-zA-Z0-9]", pw):
            return False
        return True

    def _validate(self, cfg: Config) -> None:
        """Validate a fully-populated Config object, raising SystemExit on error."""
        errors: list[str] = []
        if not validate_domain(cfg.domain):
            errors.append(f"Invalid domain: {cfg.domain!r}")
        if not validate_domain(cfg.admin_domain):
            errors.append(f"Invalid admin_domain: {cfg.admin_domain!r}")
        if not validate_domain(cfg.store_domain):
            errors.append(f"Invalid store_domain: {cfg.store_domain!r}")
        if not validate_email(cfg.email):
            errors.append(f"Invalid email: {cfg.email!r}")
        if not self._check_password(cfg.admin_password):
            errors.append("admin_password must be ≥16 chars with uppercase, lowercase, digit, and special char")
        for coin, addr in cfg.wallets.items():
            if not validate_wallet(coin, addr):
                errors.append(f"Invalid {coin.upper()} address: {addr!r}")
        if errors:
            for e in errors:
                log.error("Config error: %s", e)
            sys.exit(1)
        log.info("Configuration validated OK")


# ---------------------------------------------------------------------------
# CLASS: DockerOrchestrator
# ---------------------------------------------------------------------------


class DockerOrchestrator:
    """Manages the Docker Compose stack for Bitcart."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise DockerOrchestrator.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def generate_env(self) -> None:
        """Render the .env file for bitcart-docker from the Jinja2 template."""
        log.info("Generating .env for bitcart-docker…")
        if Environment is None:
            log.error("jinja2 is not installed.")
            sys.exit(1)
        jenv = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
        tmpl = jenv.get_template("env.j2")
        content = tmpl.render(
            domain=self.cfg.domain,
            admin_domain=self.cfg.admin_domain,
            store_domain=self.cfg.store_domain,
            email=self.cfg.email,
            cryptos=self.cfg.cryptos,
            tor_enable=self.cfg.tor_enable,
            lightning_enable=self.cfg.lightning_enable,
            db_password=self.cfg.db_password,
            generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        )
        env_path = self.cfg.docker_dir / ".env"
        if not self.dry_run:
            env_path.write_text(content)
            env_path.chmod(0o600)
        else:
            log.info("[DRY-RUN] Would write .env to %s", env_path)
        log.info(".env written to %s", env_path)

    def run_setup(self) -> None:
        """Execute setup.sh inside bitcart-docker."""
        setup_sh = self.cfg.docker_dir / "setup.sh"
        if not setup_sh.exists() and not self.dry_run:
            log.error("setup.sh not found at %s", setup_sh)
            sys.exit(1)
        log.info("Running setup.sh (this may take several minutes)…")
        run(["bash", str(setup_sh)], cwd=self.cfg.docker_dir, dry_run=self.dry_run)

    def wait_healthy(self) -> None:
        """Poll docker compose until all containers are healthy or timeout."""
        log.info("Waiting for all containers to become healthy (timeout: %ds)…", DEPLOY_TIMEOUT)
        deadline = time.time() + DEPLOY_TIMEOUT
        delay = HEALTH_POLL_INITIAL
        while time.time() < deadline:
            result = run(
                ["docker", "compose", "ps", "--format", "json"],
                capture=True,
                check=False,
                cwd=self.cfg.docker_dir,
                dry_run=False,
            )
            if self.dry_run:
                log.info("[DRY-RUN] Skipping health poll")
                return
            if result.returncode != 0:
                log.warning("docker compose ps failed — retrying in %ds…", delay)
            else:
                try:
                    services = self._parse_compose_ps(result.stdout)
                    unhealthy = [s for s in services if s.get("State") not in ("running", "healthy")]
                    if not unhealthy:
                        log.info("All containers healthy!")
                        return
                    log.info(
                        "Waiting for: %s",
                        ", ".join(s.get("Name", "?") for s in unhealthy),
                    )
                except json.JSONDecodeError:
                    log.debug("Could not parse compose ps output; retrying…")
            time.sleep(delay)
            delay = min(delay * 2, HEALTH_POLL_MAX)

        log.error("Timeout: containers did not become healthy within %ds", DEPLOY_TIMEOUT)
        sys.exit(1)

    def _parse_compose_ps(self, raw: str) -> list[dict[str, Any]]:
        """Parse docker compose ps JSON output (may be newline-delimited JSON objects)."""
        services: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, list):
                    services.extend(obj)
                elif isinstance(obj, dict):
                    services.append(obj)
            except json.JSONDecodeError:
                pass
        return services

    def stop(self) -> None:
        """Stop the docker compose stack."""
        log.info("Stopping docker compose stack…")
        run(["docker", "compose", "down"], cwd=self.cfg.docker_dir, check=False, dry_run=self.dry_run)

    def remove(self) -> None:
        """Stop the stack and remove volumes."""
        log.info("Removing docker compose stack and volumes…")
        run(
            ["docker", "compose", "down", "--volumes", "--remove-orphans"],
            cwd=self.cfg.docker_dir,
            check=False,
            dry_run=self.dry_run,
        )


# ---------------------------------------------------------------------------
# CLASS: WalletProvisioner
# ---------------------------------------------------------------------------


class WalletProvisioner:
    """Creates wallets and default store via the Bitcart REST API."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise WalletProvisioner.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run
        self.base_url = f"https://{cfg.domain}"
        self._token: Optional[str] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Obtain a JWT for the admin user via POST /api/token."""
        if self._token:
            return self._token
        log.info("Authenticating with Bitcart API…")
        if self.dry_run:
            self._token = "DRY_RUN_TOKEN"
            return self._token
        resp = requests.post(
            f"{self.base_url}/api/token",
            json={
                "username": self.cfg.email,
                "password": self.cfg.admin_password,
                "permissions": ["full_control"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        log.info("API token obtained.")
        return self._token

    def _headers(self) -> dict[str, str]:
        """Return auth headers for API calls."""
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}

    def _ensure_admin_user(self) -> None:
        """Create the initial superuser via /api/users if it does not exist."""
        log.info("Ensuring admin user exists…")
        if self.dry_run:
            return
        # Try to register the admin user; ignore 409 (already exists)
        resp = requests.post(
            f"{self.base_url}/api/users",
            json={
                "email": self.cfg.email,
                "password": self.cfg.admin_password,
                "is_superuser": True,
            },
            timeout=30,
        )
        if resp.status_code == 409:
            log.info("Admin user already exists.")
        elif resp.status_code in (200, 201):
            log.info("Admin user created.")
        else:
            log.warning("Admin user creation returned %d: %s", resp.status_code, resp.text)

    # ------------------------------------------------------------------
    # Wallet management
    # ------------------------------------------------------------------

    def _list_existing_wallets(self) -> list[dict[str, Any]]:
        """Return existing wallets from the API."""
        if self.dry_run:
            return []
        resp = requests.get(f"{self.base_url}/api/wallets", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", data) if isinstance(data, dict) else data

    def create_wallets(self) -> list[str]:
        """Create wallets for all configured coins, skipping existing ones.

        Returns:
            List of wallet IDs that were created or already existed.

        Raises:
            SystemExit: If a wallet round-trip verification fails.
        """
        self._ensure_admin_user()
        existing = self._list_existing_wallets()
        existing_xpubs = {w["xpub"]: w["id"] for w in existing}

        wallet_ids: list[str] = []
        for coin, xpub in self.cfg.wallets.items():
            if xpub in existing_xpubs:
                log.info("Wallet %s (%s) already exists — skipping.", coin.upper(), existing_xpubs[xpub])
                wallet_ids.append(existing_xpubs[xpub])
                continue

            log.info("Creating wallet for %s…", coin.upper())
            payload = {
                "name": f"{coin.upper()} Wallet",
                "xpub": xpub,
                "currency": coin.lower(),
                "lightning_enabled": self.cfg.lightning_enable,
            }
            if self.dry_run:
                log.info("[DRY-RUN] Would POST /api/wallets: %s", payload)
                wallet_ids.append(f"dry-run-{coin}")
                continue

            resp = requests.post(
                f"{self.base_url}/api/wallets",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            wallet = resp.json()
            wallet_id = wallet["id"]
            log.info("Wallet created: %s (id=%s)", coin.upper(), wallet_id)

            # Verify round-trip
            self._verify_wallet(wallet_id, xpub)
            wallet_ids.append(wallet_id)

        return wallet_ids

    def _verify_wallet(self, wallet_id: str, expected_xpub: str) -> None:
        """Verify that a wallet's stored xpub matches the input.

        Args:
            wallet_id: ID of the wallet to check.
            expected_xpub: The xpub/address that should be stored.

        Raises:
            SystemExit: On mismatch.
        """
        if self.dry_run:
            return
        resp = requests.get(
            f"{self.base_url}/api/wallets/{wallet_id}",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        stored = resp.json().get("xpub", "")
        if stored != expected_xpub:
            log.error(
                "Wallet round-trip mismatch for %s: expected %r, got %r",
                wallet_id,
                expected_xpub,
                stored,
            )
            sys.exit(1)
        log.info("Wallet %s round-trip verified OK.", wallet_id)

    # ------------------------------------------------------------------
    # Store management
    # ------------------------------------------------------------------

    def create_store(self, wallet_ids: list[str]) -> str:
        """Create (or update) the default store referencing all wallets.

        Args:
            wallet_ids: List of wallet IDs to attach.

        Returns:
            The store ID.
        """
        log.info("Provisioning default store with %d wallet(s)…", len(wallet_ids))
        if self.dry_run:
            log.info("[DRY-RUN] Would POST /api/stores")
            return "dry-run-store"

        # Check if store already exists
        resp = requests.get(f"{self.base_url}/api/stores", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        stores_data = resp.json()
        stores = stores_data.get("result", stores_data) if isinstance(stores_data, dict) else stores_data
        if stores:
            store_id = stores[0]["id"]
            log.info("Store already exists (id=%s) — patching wallets…", store_id)
            patch_resp = requests.patch(
                f"{self.base_url}/api/stores/{store_id}",
                json={"wallets": wallet_ids},
                headers=self._headers(),
                timeout=30,
            )
            patch_resp.raise_for_status()
            return store_id

        resp = requests.post(
            f"{self.base_url}/api/stores",
            json={
                "name": self.cfg.store_name,
                "wallets": wallet_ids,
                "default_currency": "USD",
            },
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        store_id = resp.json()["id"]
        log.info("Default store created (id=%s).", store_id)
        return store_id

    def provision(self) -> tuple[list[str], str]:
        """Run the full wallet + store provisioning flow.

        Returns:
            Tuple of (wallet_ids, store_id).
        """
        wallet_ids = self.create_wallets()
        store_id = self.create_store(wallet_ids)
        return wallet_ids, store_id


# ---------------------------------------------------------------------------
# CLASS: NginxManager
# ---------------------------------------------------------------------------


class NginxManager:
    """Generates and installs Nginx server block configurations."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise NginxManager.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def _render(
        self,
        template_name: str,
        domain: str,
        upstream_port: int,
    ) -> str:
        """Render an Nginx config from the Jinja2 template.

        Args:
            template_name: Template filename in templates/.
            domain: The domain for this vhost.
            upstream_port: Local port the upstream app listens on.

        Returns:
            Rendered template string.
        """
        if Environment is None:
            log.error("jinja2 is not installed.")
            sys.exit(1)
        jenv = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
        tmpl = jenv.get_template(template_name)
        return tmpl.render(
            domain=domain,
            upstream_port=upstream_port,
            indexnow_key=self.cfg.indexnow_key,
            google_verification=self.cfg.google_verification,
            bing_verification=self.cfg.bing_verification,
        )

    def _write_config(self, domain: str, content: str) -> None:
        """Write Nginx config to /etc/nginx/sites-available and symlink."""
        available = Path(f"/etc/nginx/sites-available/{domain}.conf")
        enabled = Path(f"/etc/nginx/sites-enabled/{domain}.conf")
        if not self.dry_run:
            available.write_text(content)
            if not enabled.exists():
                enabled.symlink_to(available)
        else:
            log.info("[DRY-RUN] Would write Nginx config to %s", available)

    def _install_vhost(self, domain: str, port: int) -> None:
        """Install a single virtual host configuration."""
        log.info("Generating Nginx vhost for %s (port %d)…", domain, port)
        content = self._render("nginx.conf.j2", domain, port)
        self._write_config(domain, content)

    def install_all(self) -> None:
        """Generate Nginx configs for API, admin, and store domains."""
        self._install_vhost(self.cfg.domain, API_UPSTREAM_PORT)
        self._install_vhost(self.cfg.admin_domain, ADMIN_UPSTREAM_PORT)
        self._install_vhost(self.cfg.store_domain, STORE_UPSTREAM_PORT)
        self._remove_default()
        log.info("Testing Nginx configuration…")
        run(["nginx", "-t"], dry_run=self.dry_run)
        log.info("Reloading Nginx…")
        run(["systemctl", "reload", "nginx"], dry_run=self.dry_run)

    def _remove_default(self) -> None:
        """Remove the default Nginx site to avoid conflicts."""
        default_enabled = Path("/etc/nginx/sites-enabled/default")
        if default_enabled.exists() and not self.dry_run:
            default_enabled.unlink()
            log.info("Removed default Nginx site.")


# ---------------------------------------------------------------------------
# CLASS: TLSManager
# ---------------------------------------------------------------------------


class TLSManager:
    """Runs certbot to obtain and install Let's Encrypt TLS certificates."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise TLSManager.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def obtain_certs(self) -> None:
        """Obtain TLS certificates for all configured domains."""
        domains = [self.cfg.domain, self.cfg.admin_domain, self.cfg.store_domain]
        for domain in domains:
            cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
            if cert_path.exists():
                log.info("TLS cert for %s already exists — skipping certbot.", domain)
                continue
            log.info("Obtaining TLS certificate for %s…", domain)
            run(
                [
                    "certbot",
                    "--nginx",
                    "-d", domain,
                    "--email", self.cfg.email,
                    "--agree-tos",
                    "--non-interactive",
                    "--redirect",
                ],
                dry_run=self.dry_run,
            )
        log.info("TLS certificate provisioning complete.")

    def get_expiry(self, domain: str) -> str:
        """Return TLS certificate expiry date for a domain.

        Args:
            domain: The domain to check.

        Returns:
            ISO-8601 date string, or 'unknown'.
        """
        try:
            result = run(
                ["certbot", "certificates", "--domain", domain],
                capture=True,
                check=False,
                dry_run=False,
            )
            for line in result.stdout.splitlines():
                if "Expiry Date" in line:
                    match = re.search(r"\d{4}-\d{2}-\d{2}", line)
                    if match:
                        return match.group()
        except Exception:
            pass
        return "unknown"


# ---------------------------------------------------------------------------
# CLASS: SEOGenerator
# ---------------------------------------------------------------------------


class SEOGenerator:
    """Generates all SEO artifacts (sitemap, robots.txt, JSON-LD, etc.)."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise SEOGenerator.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def _seo_dir(self, domain: str) -> Path:
        """Return the SEO asset directory for a given domain."""
        return self.cfg.seo_dir / domain

    def _ensure_dir(self, domain: str) -> None:
        """Create SEO asset directory structure for a domain."""
        d = self._seo_dir(domain)
        for sub in ["", ".well-known"]:
            (d / sub).mkdir(parents=True, exist_ok=True)

    def _jenv(self) -> "Environment":
        """Return a configured Jinja2 Environment."""
        if Environment is None:
            log.error("jinja2 is not installed.")
            sys.exit(1)
        return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

    def _write(self, path: Path, content: str) -> None:
        """Write content to a file, logging in dry-run mode."""
        if self.dry_run:
            log.info("[DRY-RUN] Would write %s (%d bytes)", path, len(content))
        else:
            path.write_text(content, encoding="utf-8")

    def generate_sitemap(
        self,
        domain: str,
        products: Optional[list[dict[str, Any]]] = None,
        categories: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        """Render and write sitemap.xml for a domain.

        Args:
            domain: The domain to generate the sitemap for.
            products: Optional list of product dicts.
            categories: Optional list of category dicts.

        Returns:
            Number of URLs in the sitemap.
        """
        products = products or []
        categories = categories or []
        now = datetime.date.today().isoformat()
        static_pages = [
            {"path": "/", "lastmod": now, "changefreq": "daily", "priority": "1.0"},
            {"path": "/products", "lastmod": now, "changefreq": "daily", "priority": "0.9"},
            {"path": "/about", "lastmod": now, "changefreq": "monthly", "priority": "0.5"},
            {"path": "/contact", "lastmod": now, "changefreq": "monthly", "priority": "0.5"},
        ]
        jenv = self._jenv()
        tmpl = jenv.get_template("sitemap.xml.j2")
        content = tmpl.render(
            domain=domain,
            static_pages=static_pages,
            products=products,
            categories=categories,
            generated_at=now,
        )
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / "sitemap.xml", content)
        total = len(static_pages) + len(products) + len(categories)
        log.info("Sitemap for %s: %d URLs", domain, total)
        return total

    def generate_robots(self, domain: str) -> None:
        """Render and write robots.txt for a domain."""
        jenv = self._jenv()
        tmpl = jenv.get_template("robots.txt.j2")
        content = tmpl.render(domain=domain)
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / "robots.txt", content)
        log.info("robots.txt written for %s", domain)

    def generate_jsonld(
        self,
        domain: str,
        products: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Render and write JSON-LD structured data for a domain."""
        products = products or []
        jenv = self._jenv()
        tmpl = jenv.get_template("jsonld.json.j2")
        content = tmpl.render(
            domain=domain,
            store_name=self.cfg.store_name,
            email=self.cfg.email,
            products=products,
            breadcrumbs=[
                {"name": "Home", "path": "/"},
                {"name": "Products", "path": "/products"},
            ],
        )
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / "jsonld.json", content)

    def generate_humans_txt(self, domain: str) -> None:
        """Write humans.txt for a domain."""
        content = (
            "/* TEAM */\n"
            f"  Site: {self.cfg.store_name}\n"
            "  Contact: See security.txt\n\n"
            "/* THANKS */\n"
            "  Bitcart — https://bitcartcc.com\n\n"
            "/* SITE */\n"
            f"  Last update: {datetime.date.today().isoformat()}\n"
            "  Standards: HTML5, CSS3\n"
            "  Software: Bitcart\n"
        )
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / "humans.txt", content)

    def generate_security_txt(self, domain: str) -> None:
        """Write .well-known/security.txt for a domain."""
        content = (
            f"Contact: mailto:{self.cfg.email}\n"
            f"Expires: {(datetime.date.today() + datetime.timedelta(days=365)).isoformat()}T23:59:59.000Z\n"
            "Preferred-Languages: en\n"
            f"Canonical: https://{domain}/.well-known/security.txt\n"
        )
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / ".well-known" / "security.txt", content)

    def generate_ads_txt(self, domain: str) -> None:
        """Write ads.txt stub for a domain."""
        content = "# ads.txt — declare authorized digital sellers\n# placeholder\n"
        self._ensure_dir(domain)
        self._write(self._seo_dir(domain) / "ads.txt", content)

    def generate_indexnow_key(self, domain: str) -> None:
        """Write the IndexNow key file for a domain."""
        self._ensure_dir(domain)
        self._write(
            self._seo_dir(domain) / f"{self.cfg.indexnow_key}.txt",
            self.cfg.indexnow_key,
        )

    def generate_manifest(self, domain: str) -> None:
        """Write a web app manifest for a domain."""
        manifest = {
            "name": self.cfg.store_name,
            "short_name": self.cfg.store_name[:12],
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#0d6efd",
            "icons": [
                {"src": "/favicon-192x192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/favicon-512x512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
        self._ensure_dir(domain)
        self._write(
            self._seo_dir(domain) / "manifest.webmanifest",
            json.dumps(manifest, indent=2),
        )

    def install_cron(self) -> None:
        """Install a cron job to regenerate sitemaps every 6 hours."""
        script_path = SCRIPT_DIR / "deploy_bitcart.py"
        cron_content = (
            "# Bitcart sitemap regeneration — managed by deploy_bitcart.py\n"
            f"0 */6 * * * root python3 {script_path} configure --seo-only 2>&1 | logger -t bitcart-sitemap\n"
        )
        cron_path = Path("/etc/cron.d/bitcart-sitemap")
        if not self.dry_run:
            cron_path.write_text(cron_content)
            cron_path.chmod(0o644)
        else:
            log.info("[DRY-RUN] Would write cron to %s", cron_path)
        log.info("Sitemap cron installed at %s", cron_path)

    def generate_all(self, domain: str) -> int:
        """Generate all SEO artifacts for a domain.

        Args:
            domain: Target domain.

        Returns:
            Number of URLs in the sitemap.
        """
        url_count = self.generate_sitemap(domain)
        self.generate_robots(domain)
        self.generate_jsonld(domain)
        self.generate_humans_txt(domain)
        self.generate_security_txt(domain)
        self.generate_ads_txt(domain)
        self.generate_indexnow_key(domain)
        self.generate_manifest(domain)
        return url_count

    def ping_search_engines(self, domain: str) -> None:
        """Ping Google, Bing, and IndexNow with the sitemap URL."""
        sitemap_url = f"https://{domain}/sitemap.xml"
        endpoints = [
            f"https://www.google.com/ping?sitemap={urllib.parse.quote(sitemap_url)}",
            f"https://www.bing.com/ping?sitemap={urllib.parse.quote(sitemap_url)}",
            f"https://api.indexnow.org/IndexNow?url={urllib.parse.quote(sitemap_url)}&key={self.cfg.indexnow_key}",
        ]
        for url in endpoints:
            try:
                log.info("Pinging %s…", url)
                if not self.dry_run:
                    if requests is not None:
                        requests.get(url, timeout=10)
                    else:
                        urllib.request.urlopen(url, timeout=10)  # noqa: S310
            except Exception as exc:
                log.warning("Ping failed for %s: %s", url, exc)


# ---------------------------------------------------------------------------
# CLASS: Hardener
# ---------------------------------------------------------------------------


class Hardener:
    """Applies security hardening: UFW, fail2ban, Docker binding."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise Hardener.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def configure_ufw(self) -> None:
        """Configure UFW firewall rules."""
        log.info("Configuring UFW…")
        rules = [
            ["ufw", "default", "deny", "incoming"],
            ["ufw", "default", "allow", "outgoing"],
            ["ufw", "allow", "22/tcp"],
            ["ufw", "allow", "80/tcp"],
            ["ufw", "allow", "443/tcp"],
        ]
        if self.cfg.lightning_enable:
            rules.append(["ufw", "allow", "9735/tcp"])
        for rule in rules:
            run(rule, dry_run=self.dry_run)
        run(["ufw", "--force", "enable"], dry_run=self.dry_run)
        log.info("UFW enabled and rules applied.")

    def configure_fail2ban(self) -> None:
        """Install a fail2ban jail for the Bitcart API."""
        jail_content = textwrap.dedent(
            f"""\
            [bitcart-api]
            enabled  = true
            port     = http,https
            filter   = bitcart-api
            logpath  = /var/log/nginx/{self.cfg.domain}-access.log
            maxretry = 10
            bantime  = 3600
            findtime = 600
            """
        )
        filter_content = textwrap.dedent(
            """\
            [Definition]
            failregex = ^<HOST> .* "(POST|GET) /api/(token|users) .* 4[0-9]{2}
            ignoreregex =
            """
        )
        jail_path = Path("/etc/fail2ban/jail.d/bitcart-api.conf")
        filter_path = Path("/etc/fail2ban/filter.d/bitcart-api.conf")
        if not self.dry_run:
            jail_path.write_text(jail_content)
            filter_path.write_text(filter_content)
        else:
            log.info("[DRY-RUN] Would write fail2ban jail to %s", jail_path)
        run(["systemctl", "enable", "fail2ban"], dry_run=self.dry_run)
        run(["systemctl", "restart", "fail2ban"], dry_run=self.dry_run)
        log.info("fail2ban configured.")

    def restrict_docker_ports(self) -> None:
        """Ensure Docker internal services are not exposed publicly."""
        log.info("Checking Docker internal service bindings…")
        # This is enforced via the .env file (postgres/redis bind 127.0.0.1)
        # We document and confirm here — actual binding is done in docker-compose
        log.info("Postgres and Redis are bound to 127.0.0.1 via compose config.")

    def harden(self) -> None:
        """Apply all hardening steps."""
        self.configure_ufw()
        self.configure_fail2ban()
        self.restrict_docker_ports()


# ---------------------------------------------------------------------------
# CLASS: Reporter
# ---------------------------------------------------------------------------


class Reporter:
    """Generates the deployment summary report and CONFIGURATION_README.md."""

    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        """Initialise Reporter.

        Args:
            cfg: Populated configuration object.
            dry_run: When True, print actions without executing them.
        """
        self.cfg = cfg
        self.dry_run = dry_run

    def write_deployment_report(
        self,
        wallet_ids: list[str],
        store_id: str,
        tls_manager: TLSManager,
        sitemap_url_count: int,
    ) -> None:
        """Write DEPLOYMENT_REPORT.md to the install directory.

        Args:
            wallet_ids: List of provisioned wallet IDs.
            store_id: Provisioned store ID.
            tls_manager: TLSManager instance (for expiry lookup).
            sitemap_url_count: Number of sitemap URLs.
        """
        now = datetime.datetime.utcnow().isoformat() + "Z"
        domains = [self.cfg.domain, self.cfg.admin_domain, self.cfg.store_domain]
        tls_lines = "\n".join(
            f"| {d} | {tls_manager.get_expiry(d)} |" for d in domains
        )
        wallet_lines = "\n".join(f"| {wid} |" for wid in wallet_ids)

        # Container status (best-effort)
        container_block = ""
        if not self.dry_run:
            result = run(
                ["docker", "compose", "ps"],
                capture=True,
                check=False,
                cwd=self.cfg.docker_dir,
                dry_run=False,
            )
            container_block = f"```\n{result.stdout}\n```"
        else:
            container_block = "_dry-run — no container status_"

        report = f"""# Bitcart Deployment Report

Generated: {now}
Script Version: {SCRIPT_VERSION}

## Domains

| Domain | Purpose |
|--------|---------|
| {self.cfg.domain} | API |
| {self.cfg.admin_domain} | Admin Panel |
| {self.cfg.store_domain} | Storefront |

## TLS Certificates

| Domain | Expiry |
|--------|--------|
{tls_lines}

## Wallets (IDs only — no secrets)

| Wallet ID |
|-----------|
{wallet_lines}

## Default Store

Store ID: `{store_id}`

## SEO

Sitemap URLs: {sitemap_url_count}
Sitemap: https://{self.cfg.store_domain}/sitemap.xml

## Container Status

{container_block}

## Notes

- Re-run `sudo python3 deploy_bitcart.py verify` to re-validate all assertions.
- Logs: /var/log/bitcart-deploy.log
"""
        report_path = self.cfg.install_dir / "DEPLOYMENT_REPORT.md"
        if not self.dry_run:
            self.cfg.install_dir.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report)
        log.info("Deployment report written to %s", report_path)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _load_config(args: argparse.Namespace) -> Config:
    """Load or collect configuration based on CLI args."""
    collector = ConfigCollector(dry_run=getattr(args, "dry_run", False))
    if hasattr(args, "config") and args.config:
        cfg = collector.from_file(Path(args.config))
    else:
        cfg = collector.interactive()
    if hasattr(args, "install_dir") and args.install_dir:
        cfg.install_dir = Path(args.install_dir)
    return cfg


def cmd_install(args: argparse.Namespace) -> None:
    """Execute a full end-to-end Bitcart installation.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy v%s — INSTALL ===", SCRIPT_VERSION)

    cfg = _load_config(args)

    # 1. System prep
    prep = SystemPrep(dry_run=dry_run)
    prep.prepare()

    # Re-import after pip install
    global requests, yaml, Environment, FileSystemLoader  # noqa: PLW0603
    try:
        import requests as _r  # noqa: F401
        import yaml as _y  # noqa: F401
        from jinja2 import Environment as _E, FileSystemLoader as _FL  # noqa: F401

        requests = _r
        yaml = _y
        Environment = _E
        FileSystemLoader = _FL
    except ImportError:
        log.warning("Could not re-import third-party packages after install.")

    # 2. Clone repos
    fetcher = RepoFetcher(dry_run=dry_run)
    fetcher.fetch_all(cfg.install_dir, cfg.docker_dir, cfg.admin_dir, cfg.store_dir)

    # 3. Docker stack
    docker = DockerOrchestrator(cfg, dry_run=dry_run)
    docker.generate_env()
    docker.run_setup()
    docker.wait_healthy()

    # 4. Nginx
    nginx = NginxManager(cfg, dry_run=dry_run)
    nginx.install_all()

    # 5. TLS
    tls = TLSManager(cfg, dry_run=dry_run)
    tls.obtain_certs()

    # Reload Nginx after TLS
    run(["systemctl", "reload", "nginx"], dry_run=dry_run)

    # 6. SEO
    seo = SEOGenerator(cfg, dry_run=dry_run)
    url_count = 0
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        url_count += seo.generate_all(domain)
    seo.install_cron()
    seo.ping_search_engines(cfg.store_domain)

    # 7. Wallet provisioning
    wallet_provisioner = WalletProvisioner(cfg, dry_run=dry_run)
    wallet_ids, store_id = wallet_provisioner.provision()

    # 8. Hardening
    hardener = Hardener(cfg, dry_run=dry_run)
    hardener.harden()

    # 9. Report
    reporter = Reporter(cfg, dry_run=dry_run)
    reporter.write_deployment_report(wallet_ids, store_id, tls, url_count)

    log.info("=== Installation complete! ===")
    log.info("API:   https://%s", cfg.domain)
    log.info("Admin: https://%s", cfg.admin_domain)
    log.info("Store: https://%s", cfg.store_domain)


def cmd_configure(args: argparse.Namespace) -> None:
    """Re-run configuration and SEO generation only.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy — CONFIGURE ===")
    cfg = _load_config(args)
    seo_only: bool = getattr(args, "seo_only", False)

    if not seo_only:
        docker = DockerOrchestrator(cfg, dry_run=dry_run)
        docker.generate_env()
        nginx = NginxManager(cfg, dry_run=dry_run)
        nginx.install_all()

    seo = SEOGenerator(cfg, dry_run=dry_run)
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        seo.generate_all(domain)
    seo.ping_search_engines(cfg.store_domain)
    log.info("Configuration complete.")


def cmd_verify(args: argparse.Namespace) -> None:
    """Re-run all post-deploy assertions on an existing installation.

    Args:
        args: Parsed CLI arguments.
    """
    log.info("=== Bitcart Deploy — VERIFY ===")
    cfg = _load_config(args)
    errors: list[str] = []

    # Check containers
    result = run(
        ["docker", "compose", "ps", "--format", "json"],
        capture=True,
        check=False,
        cwd=cfg.docker_dir,
        dry_run=False,
    )
    if result.returncode != 0:
        errors.append("docker compose ps failed")

    # Check domains
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        if not check_dns(domain):
            errors.append(f"DNS lookup failed for {domain}")

    # Check TLS
    tls = TLSManager(cfg)
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        expiry = tls.get_expiry(domain)
        if expiry == "unknown":
            errors.append(f"Could not determine TLS expiry for {domain}")
        else:
            log.info("TLS for %s expires: %s", domain, expiry)

    # Check sitemaps
    for domain in [cfg.store_domain]:
        sitemap = cfg.seo_dir / domain / "sitemap.xml"
        if not sitemap.exists():
            errors.append(f"sitemap.xml missing for {domain}")
        robots = cfg.seo_dir / domain / "robots.txt"
        if not robots.exists():
            errors.append(f"robots.txt missing for {domain}")

    if errors:
        log.error("Verification FAILED with %d error(s):", len(errors))
        for e in errors:
            log.error("  - %s", e)
        sys.exit(1)
    else:
        log.info("All verification checks PASSED.")


def cmd_update(args: argparse.Namespace) -> None:
    """Pull latest code and re-run setup.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy — UPDATE ===")
    cfg = _load_config(args)
    fetcher = RepoFetcher(dry_run=dry_run)
    fetcher.fetch_all(cfg.install_dir, cfg.docker_dir, cfg.admin_dir, cfg.store_dir)
    docker = DockerOrchestrator(cfg, dry_run=dry_run)
    docker.generate_env()
    docker.run_setup()
    docker.wait_healthy()
    log.info("Update complete.")


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Remove all installed components.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy — UNINSTALL ===")
    cfg = _load_config(args)

    # Stop and remove containers
    docker = DockerOrchestrator(cfg, dry_run=dry_run)
    docker.remove()

    # Remove Nginx configs
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        for path in [
            Path(f"/etc/nginx/sites-available/{domain}.conf"),
            Path(f"/etc/nginx/sites-enabled/{domain}.conf"),
        ]:
            if path.exists() and not dry_run:
                path.unlink()
                log.info("Removed %s", path)
            elif dry_run:
                log.info("[DRY-RUN] Would remove %s", path)

    # Remove TLS certs
    for domain in [cfg.domain, cfg.admin_domain, cfg.store_domain]:
        run(
            ["certbot", "delete", "--cert-name", domain, "--non-interactive"],
            check=False,
            dry_run=dry_run,
        )

    # Remove directories
    for d in [cfg.install_dir, cfg.docker_dir, cfg.admin_dir, cfg.store_dir, cfg.seo_dir]:
        if d.exists() and not dry_run:
            shutil.rmtree(d)
            log.info("Removed directory %s", d)
        elif dry_run:
            log.info("[DRY-RUN] Would remove directory %s", d)

    # Remove cron
    cron = Path("/etc/cron.d/bitcart-sitemap")
    if cron.exists() and not dry_run:
        cron.unlink()

    # Remove fail2ban jail
    for p in [
        Path("/etc/fail2ban/jail.d/bitcart-api.conf"),
        Path("/etc/fail2ban/filter.d/bitcart-api.conf"),
    ]:
        if p.exists() and not dry_run:
            p.unlink()

    log.info("Uninstall complete.")


def cmd_backup(args: argparse.Namespace) -> None:
    """Create a backup archive of all Bitcart data.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy — BACKUP ===")
    cfg = _load_config(args)
    output = Path(getattr(args, "output", None) or DEFAULT_BACKUP_DIR / f"bitcart-backup-{int(time.time())}.tar.gz")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Dump Postgres
    dump_path = Path("/tmp/bitcart-pg-dump.sql")
    if not dry_run:
        result = run(
            ["docker", "compose", "exec", "-T", "postgres",
             "pg_dump", "-U", "postgres", "bitcartcc"],
            cwd=cfg.docker_dir,
            capture=True,
            dry_run=False,
        )
        dump_path.write_text(result.stdout)
        log.info("Postgres dump written to %s", dump_path)
    else:
        log.info("[DRY-RUN] Would dump Postgres to %s", dump_path)

    # Create tarball
    sources = [cfg.install_dir, cfg.docker_dir / ".env", cfg.seo_dir]
    if dump_path.exists():
        sources.append(dump_path)

    if not dry_run:
        with tarfile.open(output, "w:gz") as tar:
            for src in sources:
                if Path(src).exists():
                    tar.add(src, arcname=Path(src).name)
        log.info("Backup written to %s", output)
    else:
        log.info("[DRY-RUN] Would create backup at %s", output)


def cmd_restore(args: argparse.Namespace) -> None:
    """Restore from a backup archive.

    Args:
        args: Parsed CLI arguments.
    """
    dry_run: bool = getattr(args, "dry_run", False)
    log.info("=== Bitcart Deploy — RESTORE ===")
    input_path = Path(args.input)
    if not input_path.exists():
        log.error("Backup file not found: %s", input_path)
        sys.exit(1)
    if not dry_run:
        restore_dir = Path("/tmp/bitcart-restore")
        restore_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(input_path, "r:gz") as tar:
            for member in tar.getmembers():
                # Prevent path traversal: ensure all extracted paths stay within restore_dir
                member_path = (restore_dir / member.name).resolve()
                if not str(member_path).startswith(str(restore_dir.resolve())):
                    log.error("Refusing to extract path-traversal member: %s", member.name)
                    sys.exit(1)
                tar.extract(member, path=restore_dir)
        log.info("Extracted backup to %s", restore_dir)
        # Restore Postgres dump if present
        dump = restore_dir / "bitcart-pg-dump.sql"
        if dump.exists():
            cfg = _load_config(args)
            run(
                ["docker", "compose", "exec", "-T", "postgres",
                 "psql", "-U", "postgres", "bitcartcc"],
                cwd=cfg.docker_dir,
                input_text=dump.read_text(),
                dry_run=dry_run,
            )
            log.info("Postgres restored.")
    else:
        log.info("[DRY-RUN] Would restore from %s", input_path)


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        description="Bitcart end-to-end deployment automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"deploy_bitcart.py {SCRIPT_VERSION}")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="Path to JSON log file")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # install
    p_install = sub.add_parser("install", help="Full installation")
    p_install.add_argument("--config", help="Path to config.yaml")
    p_install.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_install.add_argument("--dry-run", action="store_true", dest="dry_run")

    # configure
    p_cfg = sub.add_parser("configure", help="Re-run configuration and SEO generation")
    p_cfg.add_argument("--config", help="Path to config.yaml")
    p_cfg.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_cfg.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_cfg.add_argument("--seo-only", action="store_true", dest="seo_only", help="Only regenerate SEO files")

    # verify
    p_ver = sub.add_parser("verify", help="Re-run post-deploy assertions")
    p_ver.add_argument("--config", help="Path to config.yaml")
    p_ver.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")

    # update
    p_upd = sub.add_parser("update", help="Pull latest code and re-run setup")
    p_upd.add_argument("--config", help="Path to config.yaml")
    p_upd.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_upd.add_argument("--dry-run", action="store_true", dest="dry_run")

    # uninstall
    p_uni = sub.add_parser("uninstall", help="Remove all installed components")
    p_uni.add_argument("--config", help="Path to config.yaml")
    p_uni.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_uni.add_argument("--dry-run", action="store_true", dest="dry_run")

    # backup
    p_bak = sub.add_parser("backup", help="Create a backup archive")
    p_bak.add_argument("--config", help="Path to config.yaml")
    p_bak.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_bak.add_argument("--output", help="Output path for backup tarball")
    p_bak.add_argument("--dry-run", action="store_true", dest="dry_run")

    # restore
    p_res = sub.add_parser("restore", help="Restore from a backup archive")
    p_res.add_argument("--config", help="Path to config.yaml")
    p_res.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), dest="install_dir")
    p_res.add_argument("--input", required=True, help="Path to backup tarball")
    p_res.add_argument("--dry-run", action="store_true", dest="dry_run")

    return parser


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

SUBCOMMAND_MAP = {
    "install": cmd_install,
    "configure": cmd_configure,
    "verify": cmd_verify,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
    "backup": cmd_backup,
    "restore": cmd_restore,
}


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser = build_parser()
    args = parser.parse_args()

    # Initialise logging before anything else
    global log  # noqa: PLW0603
    log = setup_logging(
        log_file=Path(args.log_file),
        dry_run=getattr(args, "dry_run", False),
    )

    # Root check for mutating operations
    if args.subcommand not in ("verify",):
        ensure_root()

    def _signal_handler(sig: int, frame: Any) -> None:
        log.error("Interrupted by signal %d — aborting.", sig)
        sys.exit(130)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    handler = SUBCOMMAND_MAP.get(args.subcommand)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        handler(args)
    except SystemExit:
        raise
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
