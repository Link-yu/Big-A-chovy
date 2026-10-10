# -*- coding: utf-8 -*-
"""行情请求的 TLS 校验回归（R7）。

背景：历史上多处用 ``ssl._create_unverified_context()`` / ``verify=False`` 放行证书。
2026-10-03 隔离实测：东财 / 腾讯 / 新浪全部行情主机证书链由公共 CA 签发，直连与
经本机代理开启校验均可取数。因此默认必须做完整校验；确有自签 CA 需求时用
``A_SHARE_CA_BUNDLE`` 显式提供 bundle，而不是关闭校验。

本文件锁住：
1. 默认上下文是"校验开启"的（check_hostname=True / CERT_REQUIRED）；
2. CA bundle 环境变量可显式生效，且指向不存在的文件时不会被当成"不校验"；
3. 生产代码里不再残留无条件的证书放行写法（源码守卫，防止回退）。
"""

import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tls_context

PRODUCTION_SOURCES = (
    "a_share_daily_screen.py",
    "realtime_engine.py",
    "realtime_dashboard.py",
    "network_path.py",
    "tencent_kline.py",
    "web_workbench.py",
)
TOOL_SOURCES = ("query_quote.py", "query_chips.py")


class TlsContextTests(unittest.TestCase):
    def test_default_context_verifies_certificates(self):
        ctx = tls_context.build_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_requests_verify_defaults_to_true(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(tls_context.CA_BUNDLE_ENV, None)
            self.assertIs(tls_context.requests_verify(), True)

    def test_ca_bundle_env_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "ca.pem"
            # 写一个非 PEM 内容：只验证路径被采纳、加载失败也不降级为不校验
            bundle.write_text("not a pem\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {tls_context.CA_BUNDLE_ENV: str(bundle)}):
                self.assertEqual(tls_context.ca_bundle(), str(bundle))
                self.assertEqual(tls_context.requests_verify(), str(bundle))
                ctx = tls_context.build_context()
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_missing_bundle_is_not_treated_as_no_verification(self):
        with mock.patch.dict(os.environ, {tls_context.CA_BUNDLE_ENV: "/nope/does-not-exist.pem"}):
            self.assertIsNone(tls_context.ca_bundle())
            self.assertIs(tls_context.requests_verify(), True)
            ctx = tls_context.build_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


class NoInsecureTlsTests(unittest.TestCase):
    """源码守卫：这些是唯一被允许发起行情请求的模块，不得再出现证书放行。"""

    def test_no_unconditional_tls_bypass(self):
        targets = [(SCRIPT_DIR / name) for name in PRODUCTION_SOURCES]
        targets += [(SCRIPT_DIR.parent.parent / "tools" / name) for name in TOOL_SOURCES]
        for path in targets:
            with self.subTest(source=path.name):
                src = path.read_text(encoding="utf-8")
                self.assertNotIn("_create_unverified_context", src, path.name)
                self.assertNotIn("verify=False", src, path.name)
                self.assertNotIn("CERT_NONE", src, path.name)
                self.assertNotIn("check_hostname = False", src, path.name)


if __name__ == "__main__":
    unittest.main()
