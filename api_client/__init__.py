"""服务端 API 客户端包。

用法（蓝图/服务层）：
    from api_client import endpoints
    ok, data = endpoints.get_profile()
    if not ok and data.get('code') == 'ERR_NO_SERVER':
        ...  # 开发期无服务端分支
"""
from .client import APIClient, init, get_client  # noqa: F401
from . import endpoints  # noqa: F401
