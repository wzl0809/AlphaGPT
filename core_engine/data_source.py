# -*- coding: utf-8 -*-
"""本地缓存的 K 线数据源（P1 / 方案 B）。

被动触发：DataEngine.load()（训练 / 信号评估 / 回测）与 stock_context_builder
（个股 AI 上下文）在需要时调用 get_kline()——点训练/评估按钮即取或补全。

缓存策略（per-code parquet）：
  - 命中（缓存覆盖请求区间，end 侧折算到「≤end 的最近工作日」）→ 零网络直返。
  - 未命中/过期 → 全量抓取 新浪(qfq,首选,非东财) → akshare(qfq,东财) → baostock(qfq) → tushare(qfq) 并覆写缓存。

为什么「全量覆写」而非「增量追加」：akshare/baostock 一次返回整个 [start,end]
区间，没有高效的「只取尾段」；缓存的真正价值是「命中即零网络」（重复训练/评估
同一只票不再触网）。全量覆写还顺带规避 qfq 拼接 / 重锚问题（每次刷新是单源整段
qfq，因子多为相对量对锚点漂移不敏感）。

线程安全：训练线程（runner）与信号评估线程（signal._engine_lock 只罩 signal）
可能并发调用 → 模块级 _cache_lock 串行化「读-判-抓-写」临界区（带 double-check），
命中路径不加锁。

P1 范围：沪深 A 股 + ETF 日线（北交所 920 代码映射留 P2）。复权沿用各源现成 qfq，不自算。返回统一 akshare 中文列名 schema：
  日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量（akshare 成功时附带 成交额/涨跌幅 等）。

环境变量（与 AlphaGPT.Config 同名大写，可独立覆盖）：
  KLINE_CACHE_DIR（默认 kline_cache，CWD 相对，与 margin_balance 同级）
  TUSHARE_TOKEN / TUSHARE_PER_MINUTE / TUSHARE_PER_DAY（第三级兜底）
"""
import os
import time
import threading
import logging
from collections import deque

import pandas as pd

logger = logging.getLogger('AlphaGPT')   # 与 AlphaGPT.py 同 logger，日志统一进 alphagpt.log

_cache_lock = threading.Lock()

# ============================================================================
# tushare 限频与代码格式化（自 AlphaGPT.py 迁入，data_source 自包含、不反向 import）
# ============================================================================
_tushare_calls_minute: deque = deque()
_tushare_calls_today: int = 0
_tushare_last_date = None

# adj_factor 进程级缓存（code → DataFrame）。adj_factor 受 tushare 积分强限频（低积分 1次/小时），
# 且是慢变量（仅除权除息日变动）→ 取到一次就缓存复用，避免每次 _fetch_tushare 都撞限频。
_ADJ_FACTOR_CACHE: dict = {}


def _tushare_rate_limit(per_minute: int, per_day: int):
    """tushare 客户侧限频：跨 session 共享的分钟/自然日计数，超限则 sleep 或抛错。"""
    global _tushare_calls_today, _tushare_last_date, _tushare_calls_minute
    now = time.time()
    today = time.strftime('%Y-%m-%d')
    if _tushare_last_date is not None and today != _tushare_last_date:
        _tushare_calls_today = 0
        _tushare_calls_minute.clear()
    _tushare_last_date = today
    while _tushare_calls_minute and _tushare_calls_minute[0] < now - 60:
        _tushare_calls_minute.popleft()
    if per_day > 0 and _tushare_calls_today >= per_day:
        raise RuntimeError(f"tushare 当日调用已达上限 {per_day}")
    if per_minute > 0 and len(_tushare_calls_minute) >= per_minute:
        sleep_time = 60 - (now - _tushare_calls_minute[0]) + 0.5
        logger.info(f"tushare 触发分钟限频，sleep {sleep_time:.1f}s")
        time.sleep(sleep_time)
    _tushare_calls_minute.append(time.time())
    _tushare_calls_today += 1


def _tushare_code(code: str) -> str:
    """裸 6 位代码 → tushare ts_code（.SH/.SZ 后缀）。"""
    code = str(code)
    if code.startswith(('6', '51')):
        return code + '.SH'
    return code + '.SZ'


# ============================================================================
# 缓存读写
# ============================================================================
def _cache_dir() -> str:
    return os.environ.get('KLINE_CACHE_DIR') or 'kline_cache'


def _cache_path(code: str) -> str:
    return os.path.join(_cache_dir(), f"{code}.parquet")


def _read_cache(path: str):
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:
        logger.warning(f"kline 缓存读取失败({path})，将重抓: {e}")
        return None


def _write_cache(path: str, df: pd.DataFrame):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    df.to_parquet(path, engine='pyarrow', compression='snappy', index=False)


def _effective_end(end) -> pd.Timestamp:
    """请求 end 若为今天/未来/周末，折算为「≤end 的最近工作日」。
    避免 end=今天(盘前/非交易日) 导致缓存永不命中。节假日未计入（max 侧靠覆写兜底）。"""
    e = pd.to_datetime(end)
    today = pd.Timestamp.now().normalize()
    if e > today:
        e = today
    while e.weekday() > 4:          # 周六=5 周日=6 → 回退到周五
        e -= pd.Timedelta(days=1)
    return e


def _is_fresh(cached, start, end) -> bool:
    """缓存是否覆盖请求区间。
    start 侧放宽 10 天：首个交易日可能落在 start 之后（周末/春节/国庆等假期）。"""
    if cached is None or len(cached) == 0 or '日期' not in cached.columns:
        return False
    d = pd.to_datetime(cached['日期'])
    return (d.min() <= pd.to_datetime(start) + pd.Timedelta(days=10)) and (d.max() >= _effective_end(end))


def _slice_range(cached: pd.DataFrame, start, end) -> pd.DataFrame:
    d = pd.to_datetime(cached['日期'])
    mask = (d >= pd.to_datetime(start)) & (d <= pd.to_datetime(end))
    out = cached.loc[mask].copy()
    out['日期'] = pd.to_datetime(out['日期'])
    return out.sort_values('日期').reset_index(drop=True)


# ============================================================================
# 各源抓取（统一归一化为 akshare 中文 schema）
# ============================================================================
_AK_COLS = ['日期', '开盘', '最高', '最低', '收盘', '成交量']
# ⚠️成交量 canonical 单位 = 「手」（akshare stock_zh_a_hist / tushare pro_bar 原生）。
# baostock / 新浪 stock_zh_a_daily 返回的是「股」→ 各 _fetch_* 内必须 ÷100 归一到手，
# 否则源切换时同一 parquet 的成交量列静默跳变 100×（docs/12 §1）。


def _finish(df: pd.DataFrame):
    """统一收尾：日期→datetime、排序、保证核心列存在。空 df → None。"""
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df['日期'] = pd.to_datetime(df['日期'])
    for c in _AK_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df.sort_values('日期').reset_index(drop=True)


def _fetch_akshare(code, start, end, adjust='qfq'):
    """akshare qfq，5 次重试 + timeout（仅 stock_zh_a_hist 支持 timeout 形参）。"""
    import akshare as ak
    is_etf = str(code).startswith(('51', '15', '16'))
    _AK_RETRY = 5
    for attempt in range(1, _AK_RETRY + 1):
        try:
            if is_etf:
                df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                         start_date=start, end_date=end, adjust=adjust)
            else:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date=start, end_date=end, adjust=adjust, timeout=15)
            if df is not None and '日期' in df.columns and len(df) > 0:
                return _finish(df)
        except Exception:
            # 异常明细不进用户日志（避免暴露 RemoteDisconnected 等内部结构）；
            # 最终失败堆栈由 runner 写入 logs/train_error.log 供排查。
            pass
        if attempt < _AK_RETRY:
            logger.info(f"📡 正在获取「{code}」行情数据，数据源响应较慢，重试中（{attempt}/{_AK_RETRY}）")
            time.sleep(1.5 * attempt)   # 1.5s/3s/4.5s/6s
    return None


def _with_timeout(seconds, fn, *args, **kwargs):
    """线程级超时包装（akshare 部分函数无 timeout 形参，防偶发挂死）。超时返回 None。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=seconds)
    except FuturesTimeout:
        return None
    finally:
        ex.shutdown(wait=False)


def _fetch_sina(code, start, end, adjust='qfq'):
    """新浪日K（``ak.stock_zh_a_daily``，**非东财**）。东财 push2 频繁被拦/挂死，新浪稳定 ~2s
    且 qfq 完整 → 作历史K **首选**（核心 OHLCV 即训练/回测所需；东财附加列未被使用）。

    symbol 带 sh/sz 前缀（6→sh，余→sz）；start/end 归一 YYYYMMDD；复权沿用新浪现成 qfq
    （与"各源现成 qfq 不自算"策略一致）。返回 date/open/high/low/close/volume → 归一中文 schema。
    ``stock_zh_a_daily`` 无 timeout 形参 → 用 ``_with_timeout(20)`` 防挂死。ETF(51/15/16) 跳过
    （新浪是个股接口，ETF 由东财 fund_etf_hist_em→baostock 兜底）。
    """
    import akshare as ak
    code = str(code)
    if code.startswith(('51', '15', '16')):
        return None
    sym = ('sh' if code.startswith('6') else 'sz') + code
    try:
        sd = pd.to_datetime(start).strftime('%Y%m%d')
        ed = pd.to_datetime(end).strftime('%Y%m%d')
        df = _with_timeout(20, ak.stock_zh_a_daily, symbol=sym,
                           start_date=sd, end_date=ed, adjust=adjust)
    except Exception:
        # 失败明细不进用户日志（异常结构暴露）；交下一级兜底
        return None
    if df is None or len(df) == 0 or 'date' not in df.columns:
        return None
    df = df.rename(columns={'date': '日期', 'open': '开盘', 'high': '最高',
                            'low': '最低', 'close': '收盘', 'volume': '成交量'})
    # ⚠️单位归一：新浪 volume 是「股」，akshare/tushare 是「手」（训练数据 canonical）→ ÷100 对齐。
    # 否则源切换（东财挂→走新浪）时成交量静默跳变 100×，污染量价因子（docs/12 §1）。
    df['成交量'] = pd.to_numeric(df['成交量'], errors='coerce') / 100.0
    return _finish(df)


def _fetch_baostock(code, start, end):
    """baostock qfq(adjustflag=2)，登录 3 次重试 + error_code 检查 + 空 fields 守卫。"""
    import baostock as bs
    from baostock_lock import baostock_lock
    code = str(code)
    with baostock_lock:
        try:
            for attempt in range(1, 4):
                lg = bs.login()
                logger.info(f'baostock login: error_code={lg.error_code}, error_msg={lg.error_msg} (attempt {attempt}/3)')
                if str(lg.error_code) == '0':
                    break
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))   # 1s, 2s
            if str(lg.error_code) != '0':
                raise RuntimeError(f"baostock 登录失败({lg.error_code}/{lg.error_msg})")
            code_bs = ('sh.' if code.startswith(('6', '51')) else 'sz.') + code
            new_sd = pd.to_datetime(start).strftime('%Y-%m-%d')
            new_ed = pd.to_datetime(end).strftime('%Y-%m-%d')
            rs = bs.query_history_k_data_plus(
                code_bs, "date,code,open,high,low,close,volume",
                start_date=new_sd, end_date=new_ed, frequency="d", adjustflag="2")
            # 防御：未登录/空响应时 rs.fields 保持默认空 [] → 直接 DataFrame 会零列 → KeyError
            if not rs.fields or 'date' not in rs.fields:
                raise RuntimeError(f"baostock 返回空结果(fields={rs.fields}, error_code={rs.error_code})")
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            df = pd.DataFrame(data_list, columns=rs.fields)
            df = df.rename(columns={'date': '日期', 'open': '开盘', 'high': '最高',
                                    'low': '最低', 'close': '收盘', 'volume': '成交量'})
            for c in ['开盘', '最高', '最低', '收盘']:
                df[c] = pd.to_numeric(df[c], errors='coerce').ffill().bfill()
            # ⚠️单位归一：baostock volume 是「股」，akshare/tushare 是「手」（训练数据 canonical）→ ÷100 对齐（docs/12 §1）
            df['成交量'] = pd.to_numeric(df['成交量'], errors='coerce').ffill().bfill() / 100.0
            return _finish(df)
        except Exception:
            # 失败明细不进用户日志（异常结构暴露），最终失败由 runner 落本地日志
            return None
        finally:
            try:
                bs.logout()
            except Exception:
                pass


def _fetch_tushare(code, start, end):
    """tushare 日K(qfq)：pro.daily + adj_factor 手算前复权。无 token / 拿不到 adj_factor 则跳过。

    ⚠️ 不返回 raw（未复权）：adj_factor 受 tushare 积分强限频（低积分 1次/小时），拿不到时
    返回 None 让上层走其它 qfq 源或诚实失败——绝不把未复权数据混进 qfq 链污染缓存/因子
    （2026-07-23 实测：旧版回落 raw 致 tushare 层几乎全返未复权数据）。adj_factor 进程级缓存复用。
    """
    token = os.environ.get('TUSHARE_TOKEN') or ''
    if not token:
        logger.info("tushare token 未配置，跳过第三级兜底")
        return None
    try:
        import tushare as ts
        per_min = int(os.environ.get('TUSHARE_PER_MINUTE') or 50)
        per_day = int(os.environ.get('TUSHARE_PER_DAY') or 8000)
        ts.set_token(token)
        _tushare_rate_limit(per_min, per_day)
        ts_code = _tushare_code(code)
        sd = pd.to_datetime(start).strftime('%Y%m%d')
        ed = pd.to_datetime(end).strftime('%Y%m%d')
        pro = ts.pro_api()
        raw = pro.daily(ts_code=ts_code, start_date=sd, end_date=ed)
        if raw is None or raw.empty:
            logger.warning("tushare daily 返回空")
            return None
        # 前复权：qfq(t)=raw(t)×adj_factor(t)/adj_factor(latest)。adj_factor 进程级缓存（慢变量、
        # 受积分强限频）；拿不到 → 返回 None，**不返回未复权 raw**（避免污染 qfq 链）
        adj = _ADJ_FACTOR_CACHE.get(code)
        if adj is None:
            try:
                af = pro.adj_factor(ts_code=ts_code, start_date=sd, end_date=ed)
            except Exception:
                af = None
            if af is not None and not af.empty and 'adj_factor' in af.columns:
                _ADJ_FACTOR_CACHE[code] = af
                adj = af
        if adj is None or adj.empty:
            logger.info(f"tushare adj_factor 不可用(积分限频/未缓存)，{code} 跳过——不返回未复权")
            return None
        raw = raw.merge(adj[['trade_date', 'adj_factor']], on='trade_date', how='left')
        fa = pd.to_numeric(raw['adj_factor'], errors='coerce')
        latest = fa.max()   # adj_factor 累计单调递增，max=最新日（qfq 锚=最新交易日）
        if not latest or latest <= 0 or fa.isna().all():
            return None
        for c in ['open', 'high', 'low', 'close']:
            raw[c] = pd.to_numeric(raw[c], errors='coerce') * fa / latest
        # 归一中文 schema（pro.daily: 英文列, vol=手=canonical 无需换算, trade_date=YYYYMMDD）
        df = raw.rename(columns={'trade_date': '日期', 'open': '开盘', 'high': '最高',
                                 'low': '最低', 'close': '收盘', 'vol': '成交量'})
        for c in ['开盘', '最高', '最低', '收盘', '成交量']:
            df[c] = pd.to_numeric(df[c], errors='coerce').ffill().bfill()
        return _finish(df)
    except Exception:
        # 失败明细不进用户日志（异常结构暴露），最终失败由 runner 落本地日志
        return None


def _fetch_full(code, start, end, adjust='qfq'):
    """回退：新浪(非东财,首选) → akshare(东财) → baostock → tushare。
    全部失败抛 ValueError。新浪首选：东财 push2 频繁被拦/挂死（5 次重试最坏 75s+），新浪稳定 ~2s。"""
    df = _fetch_sina(code, start, end, adjust)
    if df is not None:
        return df
    df = _fetch_akshare(code, start, end, adjust)
    if df is not None:
        return df
    df = _fetch_baostock(code, start, end)
    if df is not None:
        return df
    df = _fetch_tushare(code, start, end)
    if df is not None:
        return df
    raise ValueError("未获取到数据，请检查接口调用或网络是否正常")


# ============================================================================
# 对外唯一入口
# ============================================================================
def get_kline(code, start, end, adjust='qfq'):
    """取沪深 A 股 / ETF 日线（qfq）。命中缓存零网络，否则全量抓取并缓存。

    返回 DataFrame（按日期升序；列含 日期/开盘/最高/最低/收盘/成交量，akshare 中文
    schema；akshare 成功时附带 成交额/涨跌幅 等）。全部数据源失败且无缓存 → 抛
    ValueError（与原 load() 行为一致）。
    """
    code = str(code)
    path = _cache_path(code)

    # 1) 命中（无锁，零网络）
    cached = _read_cache(path)
    if _is_fresh(cached, start, end):
        logger.info(f"kline 缓存命中 {code}（{pd.to_datetime(cached['日期']).min()}~"
                    f"{pd.to_datetime(cached['日期']).max()}），零网络直返")
        return _slice_range(cached, start, end)

    # 2) 未命中 → 加锁抓取+写（double-check 防并发重复抓）
    with _cache_lock:
        cached = _read_cache(path)
        if _is_fresh(cached, start, end):
            return _slice_range(cached, start, end)
        logger.info(f"kline 缓存未覆盖 {code}，全量抓取 {start}~{end}（新浪→akshare→baostock→tushare）")
        df = _fetch_full(code, start, end, adjust)
        _write_cache(path, df)
        return df
