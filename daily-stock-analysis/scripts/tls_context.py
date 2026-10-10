#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情请求的 TLS 上下文唯一来源。

背景（2026-10-03 隔离实测）：东财 push2/push2delay/82.push2/push2his、公告
np-anotice、腾讯 web.ifzq/ifzq、新浪 vip/money 的证书链全部由公共 CA 签发，
直连与经本机代理（127.0.0.1:7890）**开启校验**都可正常取数。因此历史上的
``ssl._create_unverified_context()`` / ``verify=False`` 是不必要的放行。

默认做完整证书校验。确有企业代理或自签 CA 需求时，把 CA bundle 路径放进
``A_SHARE_CA_BUNDLE`` 环境变量（PEM 文件），而不是关闭校验——关闭校验会让
中间人替换行情数据变得不可发现。

证书失败按「该路径/该数据不可用」处理：调用方把它当作路径不通或数据缺失，
不允许静默放行，也不允许把失败转换成「没有候选」。
"""

from __future__ import annotations

import os
import ssl
from typing import Optional

CA_BUNDLE_ENV = "A_SHARE_CA_BUNDLE"


def ca_bundle() -> Optional[str]:
    """返回可用的 CA bundle 路径；未配置或文件不存在时返回 None。"""
    raw = os.environ.get(CA_BUNDLE_ENV, "").strip()
    if not raw:
        return None
    if not os.path.isfile(raw):
        return None
    return raw


def build_context() -> ssl.SSLContext:
    """标准库/https 用的校验上下文（系统 CA，或 opt-in 的 CA bundle）。"""
    ctx = ssl.create_default_context()
    bundle = ca_bundle()
    if bundle:
        try:
            ctx.load_verify_locations(cafile=bundle)
        except (OSError, ssl.SSLError):
            # bundle 写了但读不了：保留系统 CA 校验，不降级为不校验。
            pass
    return ctx


def requests_verify():
    """requests 的 ``verify`` 参数：True，或 opt-in 的 CA bundle 路径。"""
    return ca_bundle() or True
