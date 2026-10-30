import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
GATEWAY = (ROOT / "infrastructure" / "reverse_proxy_gateway.py").read_text(encoding="utf-8")
PAGES = (ROOT / "web" / "pages.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yml").read_text(encoding="utf-8")
HELPER = (ROOT / "deploy" / "codex-provider-console-nginx-deploy").read_text(encoding="utf-8")


class ReverseProxyManagementTests(unittest.TestCase):
    def test_reverse_proxy_is_an_opt_in_hardened_compose_service(self):
        self.assertIn("reverse-proxy:", COMPOSE)
        self.assertIn('profiles: ["reverse-proxy"]', COMPOSE)
        self.assertIn("nginx:1.27-alpine", COMPOSE)
        self.assertIn("no-new-privileges:true", COMPOSE)
        self.assertIn("cap_drop:", COMPOSE)
        self.assertIn("CHOWN", COMPOSE)
        self.assertIn("DAC_OVERRIDE", COMPOSE)
        self.assertIn("NET_BIND_SERVICE", COMPOSE)
        self.assertIn("SETGID", COMPOSE)
        self.assertIn("SETUID", COMPOSE)

    def test_reverse_proxy_api_never_persists_certificate_or_private_key(self):
        self.assertIn('class ReverseProxyApplyRequest', APP)
        self.assertIn("certificate_pem", APP)
        self.assertIn("private_key_pem", APP)
        self.assertIn('settings["reverse_proxy"] = {"domain": domain, "upstream": upstream, "backend":', APP)
        self.assertIn('audit("reverse_proxy_applied", domain=domain, upstream=upstream)', APP)

    def test_reverse_proxy_has_status_apply_and_delete_operations(self):
        self.assertIn('@PROXY_ROUTER.get("/api/reverse-proxy/status")', APP)
        self.assertIn('@PROXY_ROUTER.post("/api/reverse-proxy/apply")', APP)
        self.assertIn('@PROXY_ROUTER.post("/api/reverse-proxy/delete")', APP)
        self.assertIn("app.include_router(PROXY_ROUTER)", APP)
        self.assertIn('"docker", "run", "--rm"', GATEWAY)
        self.assertIn('"nginx", "-t"', GATEWAY)
        self.assertIn('"docker", "compose", "--profile", "reverse-proxy", "up", "-d", "--force-recreate", "reverse-proxy"', GATEWAY)
        self.assertNotIn('""".format(domain=data["domain"], upstream=data["upstream"])', GATEWAY)
        self.assertIn("resolver 127.0.0.11 ipv6=off valid=30s;", GATEWAY)
        self.assertIn("proxy_pass $proxy_upstream;", GATEWAY)
        self.assertIn('backend": "system_nginx"', GATEWAY)
        self.assertIn('127.0.0.1:8787', GATEWAY)
        self.assertIn('codex-provider-console-nginx-deploy', GATEWAY)
        self.assertNotIn('--pid=host', GATEWAY)
        self.assertNotIn('nsenter', GATEWAY)
        self.assertIn("nginx-last-known-good.conf", HELPER)
        self.assertIn("systemctl reload nginx.service", HELPER)
        self.assertIn("configuration validation failed", HELPER)

    def test_reverse_proxy_ui_accepts_pem_but_does_not_load_it_back(self):
        self.assertIn('id="proxy-certificate"', PAGES)
        self.assertIn('id="proxy-private-key"', PAGES)
        self.assertIn('校验并部署', PAGES)
        self.assertNotIn('id="proxy-cert"', PAGES)
        self.assertNotIn('id="proxy-key"', PAGES)

    def test_deployed_proxy_uses_a_summary_view_until_user_edits_it(self):
        self.assertIn('id="proxy-success-view"', PAGES)
        self.assertIn('id="proxy-form-view"', PAGES)
        self.assertIn('id="proxy-edit"', PAGES)
        self.assertIn('id="proxy-cancel-edit"', PAGES)
        self.assertIn("function showProxySuccess", PAGES)
        self.assertIn("function showProxyForm", PAGES)
        self.assertIn("id='proxy-loading'", PAGES)
        self.assertIn("$('#proxy-form-view').hidden=true", PAGES)
        self.assertIn("function cancelProxyEdit", PAGES)
        self.assertIn("function abandonProxyEdit", PAGES)
        self.assertIn("if(target!=='console')abandonProxyEdit()", PAGES)
        self.assertIn("if(result.configured&&!proxyEditing)showProxySuccess(result)", PAGES)
        self.assertIn("result.backend==='system_nginx'", PAGES)

    def test_deleting_proxy_removes_server_files_and_returns_to_configuration(self):
        self.assertIn("def reverse_proxy_delete_script", GATEWAY)
        self.assertIn('"docker", "compose", "--profile", "reverse-proxy", "rm", "-sf", "reverse-proxy"', GATEWAY)
        self.assertIn("shutil.rmtree(root)", GATEWAY)
        self.assertIn('settings["reverse_proxy"] = {}', APP)
        self.assertIn("function deleteReverseProxy", PAGES)
        self.assertIn("/api/reverse-proxy/delete-progress", PAGES)
        self.assertIn("删除反向代理", PAGES)
        self.assertIn("正在删除 Nginx 配置和 TLS 文件", APP)
        self.assertIn("/api/reverse-proxy/delete", APP)
        self.assertIn("删除代理", PAGES)
        self.assertIn("def delete_reverse_proxy_after_response", APP)
        self.assertIn("background_tasks.add_task(delete_reverse_proxy_after_response)", APP)
        self.assertNotIn("await refreshReverseProxyStatus()}catch(error){note(error.message,'proxy-notice')}finally{button.disabled=false;button.textContent='删除代理'}", PAGES)
