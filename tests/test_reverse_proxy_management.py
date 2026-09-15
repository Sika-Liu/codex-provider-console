import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yml").read_text(encoding="utf-8")


class ReverseProxyManagementTests(unittest.TestCase):
    def test_reverse_proxy_is_an_opt_in_hardened_compose_service(self):
        self.assertIn("reverse-proxy:", COMPOSE)
        self.assertIn('profiles: ["reverse-proxy"]', COMPOSE)
        self.assertIn("nginx:1.27-alpine", COMPOSE)
        self.assertIn("no-new-privileges:true", COMPOSE)
        self.assertIn("cap_drop:", COMPOSE)
        self.assertIn("DAC_OVERRIDE", COMPOSE)
        self.assertIn("NET_BIND_SERVICE", COMPOSE)
        self.assertIn("SETGID", COMPOSE)
        self.assertIn("SETUID", COMPOSE)

    def test_reverse_proxy_api_never_persists_certificate_or_private_key(self):
        self.assertIn('class ReverseProxyApplyRequest', APP)
        self.assertIn("certificate_pem", APP)
        self.assertIn("private_key_pem", APP)
        self.assertIn('settings["reverse_proxy"] = {"domain": domain, "upstream": upstream}', APP)
        self.assertIn('audit("reverse_proxy_applied", domain=domain, upstream=upstream)', APP)

    def test_reverse_proxy_has_status_apply_and_disable_operations(self):
        self.assertIn('@app.get("/api/reverse-proxy/status")', APP)
        self.assertIn('@app.post("/api/reverse-proxy/apply")', APP)
        self.assertIn('@app.post("/api/reverse-proxy/disable")', APP)
        self.assertIn('"docker", "run", "--rm"', APP)
        self.assertIn('"nginx", "-t"', APP)
        self.assertIn('"docker", "compose", "--profile", "reverse-proxy", "up", "-d", "--force-recreate", "reverse-proxy"', APP)
        self.assertNotIn('""".format(domain=data["domain"], upstream=data["upstream"])', APP)
        self.assertIn("resolver 127.0.0.11 ipv6=off valid=30s;", APP)
        self.assertIn("proxy_pass $proxy_upstream;", APP)

    def test_reverse_proxy_ui_accepts_pem_but_does_not_load_it_back(self):
        self.assertIn('id="proxy-certificate"', APP)
        self.assertIn('id="proxy-private-key"', APP)
        self.assertIn('校验并部署', APP)
        self.assertNotIn('id="proxy-cert"', APP)
        self.assertNotIn('id="proxy-key"', APP)
