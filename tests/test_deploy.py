"""
tests/test_deploy.py — Unit tests for deploy_bitcart.py

Covers:
  - Wallet address / xpub validation (all supported coins)
  - Config parsing from YAML
  - Sitemap generation (URL count, XML structure)
  - Email validation
  - Domain validation
  - Password strength validation
  - ConfigCollector._validate raises SystemExit on bad input
  - SEOGenerator.generate_sitemap returns correct URL count
  - DockerOrchestrator._parse_compose_ps handles various JSON formats
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Make deploy_bitcart importable from the repo root without installation
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Pre-import optional third-party packages so that they are already in
# sys.modules before deploy_bitcart.py is loaded.  If a package is not
# installed we silently skip; deploy_bitcart.py handles the ImportError
# gracefully with its own None-guards.
for _optional_pkg in ("yaml", "requests", "jinja2"):
    try:
        __import__(_optional_pkg)
    except ImportError:
        pass

import deploy_bitcart as d  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs: object) -> d.Config:
    """Create a Config with valid defaults, overridable by kwargs."""
    cfg = d.Config()
    cfg.domain = kwargs.get("domain", "pay.example.com")
    cfg.admin_domain = kwargs.get("admin_domain", "admin.example.com")
    cfg.store_domain = kwargs.get("store_domain", "shop.example.com")
    cfg.email = kwargs.get("email", "admin@example.com")
    cfg.admin_password = kwargs.get("admin_password", "ValidPass!1234X#")
    cfg.db_password = kwargs.get("db_password", "dbpass123")
    cfg.wallets = dict(kwargs.get("wallets", {}))
    cfg.tor_enable = bool(kwargs.get("tor_enable", False))
    cfg.lightning_enable = bool(kwargs.get("lightning_enable", False))
    cfg.cryptos = list(kwargs.get("cryptos", ["btc"]))
    return cfg


# ---------------------------------------------------------------------------
# Test: validate_email
# ---------------------------------------------------------------------------

class TestValidateEmail(unittest.TestCase):
    """Tests for deploy_bitcart.validate_email()."""

    def test_valid_simple(self) -> None:
        self.assertTrue(d.validate_email("user@example.com"))

    def test_valid_subdomain(self) -> None:
        self.assertTrue(d.validate_email("user@mail.example.co.uk"))

    def test_valid_plus(self) -> None:
        self.assertTrue(d.validate_email("user+tag@example.org"))

    def test_missing_at(self) -> None:
        self.assertFalse(d.validate_email("userexample.com"))

    def test_missing_tld(self) -> None:
        self.assertFalse(d.validate_email("user@example"))

    def test_empty(self) -> None:
        self.assertFalse(d.validate_email(""))

    def test_at_only(self) -> None:
        self.assertFalse(d.validate_email("@"))


# ---------------------------------------------------------------------------
# Test: validate_domain
# ---------------------------------------------------------------------------

class TestValidateDomain(unittest.TestCase):
    """Tests for deploy_bitcart.validate_domain()."""

    def test_valid_simple(self) -> None:
        self.assertTrue(d.validate_domain("example.com"))

    def test_valid_subdomain(self) -> None:
        self.assertTrue(d.validate_domain("pay.example.com"))

    def test_valid_hyphen(self) -> None:
        self.assertTrue(d.validate_domain("my-store.example.com"))

    def test_invalid_no_dot(self) -> None:
        self.assertFalse(d.validate_domain("example"))

    def test_invalid_leading_dot(self) -> None:
        self.assertFalse(d.validate_domain(".example.com"))

    def test_invalid_empty(self) -> None:
        self.assertFalse(d.validate_domain(""))

    def test_invalid_ip(self) -> None:
        # IP addresses should fail domain validation
        self.assertFalse(d.validate_domain("192.168.1.1"))


# ---------------------------------------------------------------------------
# Test: validate_wallet
# ---------------------------------------------------------------------------

class TestValidateWallet(unittest.TestCase):
    """Tests for deploy_bitcart.validate_wallet() for every supported coin."""

    # --- BTC ---

    def test_btc_xpub(self) -> None:
        xpub = "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKp6ska5jf15duBFveqKLV7T9tCuAgrVezta88Mq6QPKP1BwSgoU6FXDdGw7uXZH2bGBEK9Ga35Gt"
        self.assertTrue(d.validate_wallet("btc", xpub))

    def test_btc_bech32(self) -> None:
        self.assertTrue(d.validate_wallet("btc", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwfvenl"))

    def test_btc_legacy(self) -> None:
        # Genesis address (valid P2PKH)
        self.assertTrue(d.validate_wallet("btc", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"))

    def test_btc_legacy_with_space_invalid(self) -> None:
        self.assertFalse(d.validate_wallet("btc", "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf Na"))

    def test_btc_invalid(self) -> None:
        self.assertFalse(d.validate_wallet("btc", "notabitcoinaddress"))

    # --- LTC ---

    def test_ltc_xpub(self) -> None:
        # Accept xpub prefix for LTC
        xpub = "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKp6ska5jf15duBFveqKLV7T9tCuAgrVezta88Mq6QPKP1BwSgoU6FXDdGw7uXZH2bGBEK9Ga35Gt"
        self.assertTrue(d.validate_wallet("ltc", xpub))

    def test_ltc_bech32(self) -> None:
        self.assertTrue(d.validate_wallet("ltc", "ltc1q7gtnpumjxmqzz33n62xeewvg2fkf2swt5flt6p"))

    def test_ltc_invalid(self) -> None:
        self.assertFalse(d.validate_wallet("ltc", ""))

    # --- BCH ---

    def test_bch_cashaddr(self) -> None:
        self.assertTrue(d.validate_wallet("bch", "bitcoincash:qr5g8jnz67n68q89n6c8yknl4xhq5jnjzguqjy4gta"))

    def test_bch_legacy(self) -> None:
        self.assertTrue(d.validate_wallet("bch", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"))

    def test_bch_invalid(self) -> None:
        self.assertFalse(d.validate_wallet("bch", "randomstring"))

    # --- XMR ---

    def test_xmr_valid(self) -> None:
        # 95-char Monero standard address starting with 4
        addr = "4" + "A" * 94
        self.assertTrue(d.validate_wallet("xmr", addr))

    def test_xmr_invalid_prefix(self) -> None:
        addr = "5" + "A" * 94
        self.assertFalse(d.validate_wallet("xmr", addr))

    def test_xmr_too_short(self) -> None:
        self.assertFalse(d.validate_wallet("xmr", "4abc"))

    # --- ETH ---

    def test_eth_valid(self) -> None:
        self.assertTrue(d.validate_wallet("eth", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))

    def test_eth_lowercase_hex(self) -> None:
        self.assertTrue(d.validate_wallet("eth", "0x" + "a" * 40))

    def test_eth_invalid_no_prefix(self) -> None:
        self.assertFalse(d.validate_wallet("eth", "d8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))

    def test_eth_invalid_short(self) -> None:
        self.assertFalse(d.validate_wallet("eth", "0xabcdef"))

    # --- USDT ---

    def test_usdt_valid(self) -> None:
        self.assertTrue(d.validate_wallet("usdt", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))

    def test_usdt_invalid(self) -> None:
        self.assertFalse(d.validate_wallet("usdt", "notanaddress"))

    # --- TRX ---

    def test_trx_valid(self) -> None:
        # Tron address: T + 33 base58 chars
        self.assertTrue(d.validate_wallet("trx", "TKczxiWfGCPjkFABMEf7bFgnFLBLSzSKTN"))

    def test_trx_invalid_prefix(self) -> None:
        self.assertFalse(d.validate_wallet("trx", "A" + "K" * 33))

    # --- Unknown coin (accept anything non-empty) ---

    def test_unknown_coin_nonempty(self) -> None:
        self.assertTrue(d.validate_wallet("zec", "t1foobaraddress"))

    def test_unknown_coin_empty(self) -> None:
        self.assertFalse(d.validate_wallet("zec", ""))


# ---------------------------------------------------------------------------
# Test: ConfigCollector._check_password
# ---------------------------------------------------------------------------

class TestPasswordValidation(unittest.TestCase):
    """Tests for password strength validation in ConfigCollector."""

    def setUp(self) -> None:
        self.collector = d.ConfigCollector(dry_run=True)

    def test_valid_password(self) -> None:
        self.assertTrue(self.collector._check_password("ValidPass!1234X#"))

    def test_too_short(self) -> None:
        self.assertFalse(self.collector._check_password("Short!1A"))

    def test_no_uppercase(self) -> None:
        self.assertFalse(self.collector._check_password("validpass!1234xx"))

    def test_no_lowercase(self) -> None:
        self.assertFalse(self.collector._check_password("VALIDPASS!1234XX"))

    def test_no_digit(self) -> None:
        self.assertFalse(self.collector._check_password("ValidPassXXXX!!##"))

    def test_no_special(self) -> None:
        self.assertFalse(self.collector._check_password("ValidPass12341234"))

    def test_exactly_16_chars(self) -> None:
        self.assertTrue(self.collector._check_password("ValidPass!1234X#"))


# ---------------------------------------------------------------------------
# Test: ConfigCollector._validate
# ---------------------------------------------------------------------------

class TestConfigValidation(unittest.TestCase):
    """Tests that ConfigCollector._validate raises SystemExit on bad configs."""

    def setUp(self) -> None:
        self.collector = d.ConfigCollector(dry_run=True)

    def test_valid_config_passes(self) -> None:
        cfg = _make_config()
        # Should not raise
        self.collector._validate(cfg)

    def test_bad_domain_raises(self) -> None:
        cfg = _make_config(domain="not_a_domain")
        with self.assertRaises(SystemExit):
            self.collector._validate(cfg)

    def test_bad_email_raises(self) -> None:
        cfg = _make_config(email="notanemail")
        with self.assertRaises(SystemExit):
            self.collector._validate(cfg)

    def test_weak_password_raises(self) -> None:
        cfg = _make_config(admin_password="weak")
        with self.assertRaises(SystemExit):
            self.collector._validate(cfg)

    def test_bad_wallet_raises(self) -> None:
        cfg = _make_config(wallets={"btc": "notabitcoinaddress"})
        with self.assertRaises(SystemExit):
            self.collector._validate(cfg)


# ---------------------------------------------------------------------------
# Test: ConfigCollector.from_file
# ---------------------------------------------------------------------------

class TestConfigFromFile(unittest.TestCase):
    """Tests for YAML-based configuration loading."""

    VALID_YAML = textwrap.dedent(
        """\
        domain: pay.example.com
        admin_domain: admin.example.com
        store_domain: shop.example.com
        email: admin@example.com
        store_name: Test Store
        admin_password: "ValidPass!1234X#"
        db_password: "dbpassword"
        tor_enable: false
        lightning_enable: false
        wallets:
          btc: "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKp6ska5jf15duBFveqKLV7T9tCuAgrVezta88Mq6QPKP1BwSgoU6FXDdGw7uXZH2bGBEK9Ga35Gt"
        """
    )

    def test_load_valid_config(self) -> None:
        try:
            import yaml as real_yaml  # noqa: PLC0415
        except ImportError:
            self.skipTest("pyyaml not installed")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(self.VALID_YAML)
            tmp_path = Path(f.name)
        try:
            collector = d.ConfigCollector(dry_run=True)
            cfg = collector.from_file(tmp_path)
            self.assertEqual(cfg.domain, "pay.example.com")
            self.assertEqual(cfg.email, "admin@example.com")
            self.assertIn("btc", cfg.wallets)
            self.assertFalse(cfg.tor_enable)
            self.assertFalse(cfg.lightning_enable)
            self.assertEqual(cfg.store_name, "Test Store")
        finally:
            tmp_path.unlink()

    def test_missing_file_raises(self) -> None:
        try:
            import yaml  # noqa: PLC0415, F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        collector = d.ConfigCollector(dry_run=True)
        with self.assertRaises((FileNotFoundError, SystemExit)):
            collector.from_file(Path("/nonexistent/path/config.yaml"))


# ---------------------------------------------------------------------------
# Test: SEOGenerator.generate_sitemap
# ---------------------------------------------------------------------------

class TestSEOGenerator(unittest.TestCase):
    """Tests for SEOGenerator sitemap generation."""

    def _make_seo(self, tmpdir: Path) -> d.SEOGenerator:
        cfg = _make_config()
        cfg.store_domain = "shop.example.com"
        cfg.seo_dir = tmpdir
        cfg.store_name = "Test Store"
        return d.SEOGenerator(cfg, dry_run=False)

    def test_sitemap_url_count_no_products(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401

            with tempfile.TemporaryDirectory() as tmp:
                seo = self._make_seo(Path(tmp))
                count = seo.generate_sitemap("shop.example.com")
            # 4 static pages + 0 products + 0 categories
            self.assertEqual(count, 4)
        except ImportError:
            self.skipTest("jinja2 not installed")

    def test_sitemap_url_count_with_products(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401

            products = [{"id": "p1", "name": "Widget"}, {"id": "p2", "name": "Gadget"}]
            categories = [{"slug": "electronics"}]
            with tempfile.TemporaryDirectory() as tmp:
                seo = self._make_seo(Path(tmp))
                count = seo.generate_sitemap(
                    "shop.example.com",
                    products=products,
                    categories=categories,
                )
            # 4 static + 2 products + 1 category
            self.assertEqual(count, 7)
        except ImportError:
            self.skipTest("jinja2 not installed")

    def test_sitemap_file_is_valid_xml(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401
            import xml.etree.ElementTree as ET  # noqa: PLC0415, N817

            with tempfile.TemporaryDirectory() as tmp:
                seo = self._make_seo(Path(tmp))
                seo.generate_sitemap("shop.example.com")
                sitemap_path = Path(tmp) / "shop.example.com" / "sitemap.xml"
                self.assertTrue(sitemap_path.exists())
                tree = ET.parse(sitemap_path)
                root = tree.getroot()
                # Root element should be urlset
                self.assertIn("urlset", root.tag)
        except ImportError:
            self.skipTest("jinja2 not installed")

    def test_robots_contains_sitemap(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401

            with tempfile.TemporaryDirectory() as tmp:
                seo = self._make_seo(Path(tmp))
                seo.generate_robots("shop.example.com")
                robots_path = Path(tmp) / "shop.example.com" / "robots.txt"
                self.assertTrue(robots_path.exists())
                content = robots_path.read_text()
                self.assertIn("Sitemap:", content)
                self.assertIn("shop.example.com", content)
        except ImportError:
            self.skipTest("jinja2 not installed")

    def test_generate_all_returns_count(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401

            with tempfile.TemporaryDirectory() as tmp:
                seo = self._make_seo(Path(tmp))
                count = seo.generate_all("shop.example.com")
                self.assertGreaterEqual(count, 4)
        except ImportError:
            self.skipTest("jinja2 not installed")


# ---------------------------------------------------------------------------
# Test: DockerOrchestrator._parse_compose_ps
# ---------------------------------------------------------------------------

class TestDockerOrchestrator(unittest.TestCase):
    """Tests for DockerOrchestrator JSON parsing utilities."""

    def setUp(self) -> None:
        cfg = _make_config()
        self.orchestrator = d.DockerOrchestrator(cfg, dry_run=True)

    def test_parse_json_array(self) -> None:
        raw = json.dumps(
            [
                {"Name": "backend", "State": "running"},
                {"Name": "postgres", "State": "running"},
            ]
        )
        services = self.orchestrator._parse_compose_ps(raw)
        self.assertEqual(len(services), 2)
        self.assertEqual(services[0]["Name"], "backend")

    def test_parse_ndjson(self) -> None:
        lines = [
            json.dumps({"Name": "backend", "State": "running"}),
            json.dumps({"Name": "postgres", "State": "running"}),
        ]
        raw = "\n".join(lines)
        services = self.orchestrator._parse_compose_ps(raw)
        self.assertEqual(len(services), 2)

    def test_parse_empty(self) -> None:
        services = self.orchestrator._parse_compose_ps("")
        self.assertEqual(services, [])

    def test_parse_invalid_json(self) -> None:
        services = self.orchestrator._parse_compose_ps("not json at all")
        self.assertEqual(services, [])

    def test_unhealthy_detected(self) -> None:
        raw = json.dumps(
            [
                {"Name": "backend", "State": "running"},
                {"Name": "redis", "State": "starting"},
            ]
        )
        services = self.orchestrator._parse_compose_ps(raw)
        unhealthy = [s for s in services if s.get("State") not in ("running", "healthy")]
        self.assertEqual(len(unhealthy), 1)
        self.assertEqual(unhealthy[0]["Name"], "redis")


# ---------------------------------------------------------------------------
# Test: generate_password
# ---------------------------------------------------------------------------

class TestGeneratePassword(unittest.TestCase):
    """Tests for the generate_password utility."""

    def test_default_length(self) -> None:
        pw = d.generate_password()
        self.assertEqual(len(pw), 32)

    def test_custom_length(self) -> None:
        pw = d.generate_password(24)
        self.assertEqual(len(pw), 24)

    def test_uniqueness(self) -> None:
        passwords = {d.generate_password() for _ in range(20)}
        # All 20 should be distinct
        self.assertEqual(len(passwords), 20)


# ---------------------------------------------------------------------------
# Test: Config.to_dict (password redaction)
# ---------------------------------------------------------------------------

class TestConfigToDict(unittest.TestCase):
    """Tests that Config.to_dict redacts sensitive fields."""

    def test_passwords_redacted(self) -> None:
        cfg = _make_config(admin_password="S3cr3t!Pass#2024", db_password="dbpw!!")
        d_out = cfg.to_dict()
        self.assertEqual(d_out["admin_password"], "***REDACTED***")
        self.assertEqual(d_out["db_password"], "***REDACTED***")

    def test_domain_present(self) -> None:
        cfg = _make_config()
        d_out = cfg.to_dict()
        self.assertEqual(d_out["domain"], "pay.example.com")


# ---------------------------------------------------------------------------
# Test: WalletProvisioner._verify_wallet (dry-run no-op)
# ---------------------------------------------------------------------------

class TestWalletProvisioner(unittest.TestCase):
    """Tests for WalletProvisioner dry-run behaviour."""

    def setUp(self) -> None:
        cfg = _make_config()
        self.provisioner = d.WalletProvisioner(cfg, dry_run=True)

    def test_verify_wallet_dry_run_noop(self) -> None:
        # Should not raise or make any network calls in dry-run mode
        self.provisioner._verify_wallet("fake-id", "fake-xpub")

    def test_get_token_dry_run(self) -> None:
        token = self.provisioner._get_token()
        self.assertEqual(token, "DRY_RUN_TOKEN")

    def test_create_wallets_dry_run(self) -> None:
        cfg = _make_config(
            wallets={"btc": "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKp6ska5jf15duBFveqKLV7T9tCuAgrVezta88Mq6QPKP1BwSgoU6FXDdGw7uXZH2bGBEK9Ga35Gt"}
        )
        p = d.WalletProvisioner(cfg, dry_run=True)
        wallet_ids = p.create_wallets()
        self.assertEqual(len(wallet_ids), 1)
        self.assertIn("btc", wallet_ids[0])


# ---------------------------------------------------------------------------
# Test: NginxManager (dry-run, no filesystem writes)
# ---------------------------------------------------------------------------

class TestNginxManager(unittest.TestCase):
    """Tests for NginxManager dry-run rendering."""

    def test_install_all_dry_run(self) -> None:
        try:
            from jinja2 import Environment  # noqa: PLC0415,F401

            cfg = _make_config()
            mgr = d.NginxManager(cfg, dry_run=True)
            # Should not raise and should not touch filesystem
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                mgr.install_all()
        except ImportError:
            self.skipTest("jinja2 not installed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
