# -*- coding: utf-8 -*-
"""客户端本地 SQLite 模型（SQLAlchemy）。

对应 docs/02-数据库设计.md §5。共 8 张表：
  LocalFormula / QuantTracking / AIAnalysisCache / NotificationCache /
  UserProfileCache / ReportsIndex / CheckpointsIndex / SilentUploadQueue
"""
from datetime import datetime

from web.extensions import db


# ── 来源枚举（公式不可手写，仅 4 类来源）──
SOURCE_SELF = 'self'
SOURCE_BOUGHT = 'bought'
SOURCE_RELAY = 'relay'
SOURCE_BOUNTY = 'bounty'
FORMULA_SOURCES = (SOURCE_SELF, SOURCE_BOUGHT, SOURCE_RELAY, SOURCE_BOUNTY)


class _OwnedQueryMixin:
    """按属主过滤的查询起点（[[client-per-user-data-partition]]）。

    本地数据按登录账号隔离：所有「人属」表带 owner_email 列，查询一律经 .owned()
    起步 = 仅当前登录用户的数据。客户端单用户/进程，active_email() 在请求与后台线程
    均可用（login_session 进程级置位）。机器级数据（hw_cache/kline_cache）不经此机制。
    """
    @classmethod
    def owned(cls):
        from web.auth import active_email
        return cls.query.filter(cls.owner_email == active_email())


class LocalFormula(db.Model, _OwnedQueryMixin):
    """个人公式库（核心表）。

    png 路径名存库，png 文件留本地 reports/，不上传服务器。
    """
    __tablename__ = 'local_formula'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    stock_code = db.Column(db.String(16), index=True, nullable=False)
    stock_name = db.Column(db.String(64), default='')
    formula_str = db.Column(db.Text, nullable=False)
    tokens = db.Column(db.JSON)              # token 序列
    factors = db.Column(db.Text)             # 逗号分隔因子名
    ai_name = db.Column(db.String(128), default='')
    natural_language = db.Column(db.Text, default='')   # 公式自然语言解释

    # 核心指标（OOS）
    test_sharpe = db.Column(db.Float, index=True, default=0.0)
    ann_ret = db.Column(db.Float, default=0.0)
    max_dd = db.Column(db.Float, default=0.0)
    win_rate = db.Column(db.Float, default=0.0)
    calmar = db.Column(db.Float, default=0.0)

    # 来源（仅 4 类，不可手写）
    source = db.Column(db.String(16), index=True, default=SOURCE_SELF)
    origin_id = db.Column(db.String(64), default='')   # 服务端 train_record/order/bid/relay id

    # 本地产物路径
    png_path = db.Column(db.String(512), default='')
    oos_md_path = db.Column(db.String(512), default='')
    train_metrics_path = db.Column(db.String(512), default='')

    # 训练配置
    seed = db.Column(db.Integer, default=42)
    seed_strategy = db.Column(db.String(16), default='A')
    train_params = db.Column(db.JSON)        # 全量参数
    hardware_summary = db.Column(db.JSON)
    train_duration_sec = db.Column(db.Float, default=0.0)
    trained_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 状态
    in_quant = db.Column(db.Boolean, default=False, index=True)   # 是否加入量化跟踪
    saved = db.Column(db.Boolean, default=True, index=True)       # 是否已确认保存（防重复）
    server_claimed = db.Column(db.Boolean, default=False)          # 服务端库位是否已确认（False=离线 pending，待 reconcile）
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # 关联量化跟踪（反向）
    tracking = db.relationship('QuantTracking', backref='formula',
                               cascade='all, delete-orphan')


class QuantTracking(db.Model, _OwnedQueryMixin):
    """量化跟踪列表（从公式库「加入量化」生成）。"""
    __tablename__ = 'quant_tracking'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    formula_id = db.Column(db.Integer, db.ForeignKey('local_formula.id'), nullable=False, index=True)
    stock_code = db.Column(db.String(16), index=True)
    stock_name = db.Column(db.String(64), default='')
    last_signal = db.Column(db.String(16), default='')          # buy/sell/hold
    last_signal_date = db.Column(db.Date)                       # 信号目标日（下一交易日）
    last_signal_basis_date = db.Column(db.Date)                 # 信号实际基于哪天收盘（新鲜度判断）
    last_hit_rate = db.Column(db.Float)                         # 近 20 日前向命中率（真·准确度）
    last_confidence = db.Column(db.Float)                       # 信号强度 |tanh(因子末值)|（非准确率）
    last_factor_value = db.Column(db.Float)                     # 公式因子末值（纯数值，合规圆圈显示用；sign(tanh)判方向）
    last_ai_score = db.Column(db.Float)                         # DeepSeek sentiment_score 0-100（右弧）
    last_signal_fresh = db.Column(db.Boolean, default=False)    # 末根 bar 是否=今日收盘
    last_regen_at = db.Column(db.DateTime)                       # 基准图重生成时间
    deepseek_last_text = db.Column(db.Text, default='')
    tavily_last_text = db.Column(db.Text, default='')
    qmt_configured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AIAnalysisCache(db.Model, _OwnedQueryMixin):
    """deepseek / tavily 分析缓存（TTL）。"""
    __tablename__ = 'ai_analysis_cache'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    stock_code = db.Column(db.String(16), index=True)
    provider = db.Column(db.String(16), index=True)             # deepseek/tavily
    scope = db.Column(db.String(16), index=True)                # market/stock
    content = db.Column(db.Text)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, index=True)
    __table_args__ = (db.Index('ix_ai_stock_provider_scope',
                               'stock_code', 'provider', 'scope'),)


class NotificationCache(db.Model, _OwnedQueryMixin):
    """通知本地缓存（轮询拉取后缓存）。"""
    __tablename__ = 'notification_cache'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    remote_id = db.Column(db.String(64), index=True)            # 服务端 id
    kind = db.Column(db.String(16), index=True)                 # announcement/personal
    title = db.Column(db.String(256))
    content = db.Column(db.Text)
    category = db.Column(db.String(16), default='normal')       # normal/urgent/important
    is_read = db.Column(db.Boolean, default=False, index=True)
    publish_at = db.Column(db.DateTime)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)


class UserProfileCache(db.Model):
    """登录用户信息缓存（单例 id=1）。"""
    __tablename__ = 'user_profile_cache'

    id = db.Column(db.Integer, primary_key=True, default=1)
    email = db.Column(db.String(128))
    username = db.Column(db.String(64))
    user_id_remote = db.Column(db.String(64))
    level = db.Column(db.Integer, default=1)
    nexus_balance = db.Column(db.Float, default=0.0)
    subscribe_expire = db.Column(db.DateTime)
    hardware_summary = db.Column(db.JSON)
    last_poll_ts = db.Column(db.DateTime, default=datetime.utcnow)   # 通知轮询游标
    last_sync_at = db.Column(db.DateTime, default=datetime.utcnow)


class ReportsIndex(db.Model, _OwnedQueryMixin):
    """reports/ 文件索引（清理页用）。"""
    __tablename__ = 'reports_index'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    filepath = db.Column(db.String(512), unique=True, index=True)
    kind = db.Column(db.String(8), index=True)                  # txt/md/png
    stock_code = db.Column(db.String(16), index=True)
    size_bytes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class CheckpointsIndex(db.Model, _OwnedQueryMixin):
    """checkpoints/*.ckpt.pt 索引（清理页用）。"""
    __tablename__ = 'checkpoints_index'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离）
    filepath = db.Column(db.String(512), unique=True, index=True)
    stock_code = db.Column(db.String(16), index=True)
    run_id = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class SilentUploadQueue(db.Model, _OwnedQueryMixin):
    """同步待传队列（网络失败时入队，后台重传）。"""
    __tablename__ = 'silent_upload_queue'

    id = db.Column(db.Integer, primary_key=True)
    owner_email = db.Column(db.String(128), index=True, default='')   # 属主 email（账号隔离，防 A 的 payload 用 B 的 token 上传）
    payload = db.Column(db.JSON)                                # 同步记录全量元数据（无图片）
    status = db.Column(db.String(16), default='pending', index=True)   # pending/success/failed
    retries = db.Column(db.Integer, default=0)
    last_error = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    next_retry_at = db.Column(db.DateTime, index=True)


class ReleaseInfoCache(db.Model):
    """客户端版本发布信息缓存（单例 id=1）。

    latest_payload：最近一次 /api/release/info 成功结果（关于页/角标展示）。
    outdated_payload：最近一次 426 拒登 payload（更新中心零网络首屏渲染，docs/14 §5）。

    全新表 → create_all 即建（无需 _ensure 加列助手；仅"给已存在表补列"才需要）。
    """
    __tablename__ = 'release_info_cache'

    id = db.Column(db.Integer, primary_key=True, default=1)
    latest_payload = db.Column(db.JSON)
    outdated_payload = db.Column(db.JSON)
    last_checked_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
