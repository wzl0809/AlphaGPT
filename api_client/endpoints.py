# -*- coding: utf-8 -*-
"""服务端 REST 端点封装。

P01 骨架：定义方法签名，转发到 APIClient.request。
具体端点实现随 P08~P11 服务端联调逐步填充（先留调用入口，避免客户端写死）。
"""
from .client import get_client


def _call(method, path, **kwargs):
    return get_client().request(method, path, **kwargs)


# ── 鉴权 ──
def login(email: str, password: str):
    return _call('POST', '/api/auth/login', json={'email': email, 'password': password})


def release_info(client_version: str = 'unknown', channel: str = 'stable'):
    """版本检查 + 服务端判定（GET /api/release/info?cv=&channel=）。匿名端点；has_server() 时短路返回。"""
    return _call('GET', '/api/release/info',
                 params={'cv': client_version or 'unknown', 'channel': channel or 'stable'})


def register_request(email: str, pow_payload=None):
    payload = {'email': email}
    if pow_payload:
        payload['pow'] = pow_payload
    return _call('POST', '/api/auth/register/request', json=payload)


def register_confirm(email: str, code: str, username: str, password: str):
    return _call('POST', '/api/auth/register/confirm',
                 json={'email': email, 'code': code, 'username': username, 'password': password})


def register_info():
    """注册页域名下拉分组 + 引导文案（免鉴权，供页面渲染与前端即时校验）。"""
    return _call('GET', '/api/auth/register/info')


def check_username(username: str):
    """注册前昵称可用性检测（免鉴权，限流 username_check）。返回 {available, message}。"""
    return _call('GET', '/api/auth/register/check-username', params={'username': username})


def password_reset_request(email: str, pow_payload=None):
    """找回密码·发重置码（服务端统一中性文案防枚举；每邮箱 24h 一次）。"""
    payload = {'email': email}
    if pow_payload:
        payload['pow'] = pow_payload
    return _call('POST', '/api/auth/password/reset/request', json=payload)


def password_reset_confirm(email: str, code: str, new_password: str):
    """找回密码·凭重置码设新密码（成功后服务端吊销所有会话）。"""
    return _call('POST', '/api/auth/password/reset/confirm',
                 json={'email': email, 'code': code, 'new_password': new_password})


# ── 用户 / 配置 ──
def get_profile():
    return _call('GET', '/api/users/me')


def update_profile(payload: dict):
    """更新个人信息（手机/QQ/微信/头像），PATCH。"""
    return _call('PATCH', '/api/users/me', json=payload)


def change_password(old_password: str, new_password: str):
    """修改密码（校验旧密码 + 新密码强度），POST。"""
    return _call('POST', '/api/users/password',
                 json={'old_password': old_password, 'new_password': new_password})


def get_hidden_config():
    return _call('GET', '/api/config/hidden')


# ── 通知 ──
def poll_notifications(since_iso: str = ''):
    return _call('GET', '/api/notifications/poll', params={'since': since_iso})


# ── 公式 / 训练记录 ──
def upload_train_record(payload: dict):
    return _call('POST', '/api/formulas/train-records', json=payload)


# ── 公式库容量（服务端权威计数；在线硬校验 + 重连对账）──
def claim_formula_slot(local_ref: str, source: str = 'self'):
    """占一个库位（在线硬校验）。免费超额 → (False, {code:'ERR_FORMULA_LIMIT', used, limit})。"""
    return _call('POST', '/api/formulas/library/claim',
                 json={'local_ref': str(local_ref), 'source': source})


def release_formula_slot(local_ref: str):
    """释放库位（删公式时）。best-effort：失败/离线由 reconcile 孤儿清理兜底。"""
    return _call('POST', '/api/formulas/library/release', json={'local_ref': str(local_ref)})


def reconcile_library(claims: list):
    """全量对账客户端 saved 列表（按 sharpe desc 排序）。

    返回 (ok, {claimed:[...], rejected:[...], used, limit})；rejected 为超额需锁定项。
    """
    return _call('POST', '/api/formulas/library/reconcile', json={'claims': claims or []})


# ── 策略贴板 ──
def get_listings(params: dict):
    return _call('GET', '/api/trade/listings', params=params)


def get_listing(listing_id):
    """挂单详情（售前隐藏公式明文与 AI 名）。"""
    return _call('GET', f'/api/trade/listings/{listing_id}')


def create_listing(payload: dict):
    return _call('POST', '/api/trade/listings', json=payload)


def remove_listing(listing_id):
    """卖家下架自己的挂单。"""
    return _call('POST', f'/api/trade/listings/{listing_id}/remove')


def create_order(listing_id):
    """购买 → 分账 → 返回公式明文 + ai_name。"""
    return _call('POST', '/api/trade/orders', json={'listing_id': listing_id})


def get_my_orders(params: dict):
    """我的成交记录（买/卖）。"""
    return _call('GET', '/api/trade/orders', params=params)


def get_my_listings(params: dict):
    """我的挂单（卖家视角，全部状态；?status=active/sold/removed 可过滤）。"""
    return _call('GET', '/api/trade/my-listings', params=params)


# ── 悬赏 ──
def list_bounty(params: dict):
    return _call('GET', '/api/bounty/', params=params)


def get_bounty(bounty_id):
    """单个悬赏详情。"""
    return _call('GET', f'/api/bounty/{bounty_id}')


def get_my_bounties(params: dict):
    """我的悬赏（poster 视角，全部状态；响应含 frozen_total 已托管总额）。"""
    return _call('GET', '/api/bounty/my-bounties', params=params)


def create_bounty(payload: dict):
    """发单（扣押金 reward_nexus）。"""
    return _call('POST', '/api/bounty/', json=payload)


def list_bounty_bids(bounty_id):
    """接单列表（仅 test_sharpe，无公式明文）。"""
    return _call('GET', f'/api/bounty/{bounty_id}/bids')


def create_bounty_bid(bounty_id, payload: dict):
    """接单：发明文 formula_str/tokens/test_sharpe/ai_name（+可选 hardware_summary），服务端加密入库。"""
    return _call('POST', f'/api/bounty/{bounty_id}/bid', json=payload)


def award_bounty(bounty_id, bid_id):
    """中标 → 赏金给中标者 → 返回中标公式明文。"""
    return _call('POST', f'/api/bounty/{bounty_id}/award', json={'bid_id': bid_id})


def get_bounty_solution(bounty_id):
    """悬赏者拉中标公式明文（completed 后）。"""
    return _call('GET', f'/api/bounty/{bounty_id}/solution')


def cancel_bounty(bounty_id):
    """取消（仅 open 无中标，退款）。"""
    return _call('DELETE', f'/api/bounty/{bounty_id}/cancel')


# ── 接力 ──
def list_relay(params: dict):
    return _call('GET', '/api/relay/', params=params)


def get_relay(relay_id):
    """单个接力详情。"""
    return _call('GET', f'/api/relay/{relay_id}')


def create_relay(payload: dict):
    """发起（预扣 reward_per_participant × max_participants 为 escrow）。"""
    return _call('POST', '/api/relay/', json=payload)


def join_relay(relay_id, payload: dict):
    """报名（+可选 hardware_summary）。"""
    return _call('POST', f'/api/relay/{relay_id}/join', json=payload)


def submit_relay(relay_id, payload: dict):
    """提交本机结果：发明文 formula_str/tokens/test_sharpe/ai_name，服务端按 min_sharpe 判达标。"""
    return _call('POST', f'/api/relay/{relay_id}/submit', json=payload)


def finalize_relay(relay_id):
    """截止汇总（escrow 发奖 + 余额退回发起人）。"""
    return _call('POST', f'/api/relay/{relay_id}/finalize')


def get_relay_best(relay_id):
    """达标参与者拉最优公式明文（completed 后）。"""
    return _call('GET', f'/api/relay/{relay_id}/best')


def cancel_relay(relay_id):
    """取消（全额退还 escrow）。"""
    return _call('DELETE', f'/api/relay/{relay_id}/cancel')


# ── 经济（算力余额查询；充值/订阅入口已下线，服务定制走 /donate 联系页）──
def get_balance():
    return _call('GET', '/api/economy/balance')


# ── 训练结果同步端点 ──
def upload_premium(payload: dict):
    return _call('POST', '/api/sync/training', json=payload)
