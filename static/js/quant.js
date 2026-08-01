/* ============================================================================
   quant.js —— 量化信号面板交互（SocketIO 信号生成 + QMT 弹窗）
   ============================================================================ */
(function () {
    const socket = window.AlphaGPT?.socket;
    let _inflight = 0;   // 进行中的信号生成数（驱动全局 busy 防误关）

    // HTML 转义（新闻标题/URL 来自 Tavily 外部数据，防 XSS）
    const _esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    // 近期动态 → 可点击新闻链接列表（标题点开新页打开原文；兼容旧字符串格式与新 {title,url} 格式）
    function renderNewsHtml(news) {
        if (!Array.isArray(news) || !news.length) return '近期动态：暂无相关新闻';
        const items = news.map(n => {
            const title = (typeof n === 'string') ? n : ((n && n.title) || '');
            const url = ((n && typeof n === 'object') ? n.url : '') || '';
            const t = _esc(title.slice(0, 60)) || '（无标题）';
            return url ? `• <a href="${_esc(url)}" target="_blank" rel="noopener noreferrer">${t}</a>` : `• ${t}`;
        }).join('<br>');
        return `近期动态（${news.length}）<br>${items}`;
    }

    /* ── 生成信号 ── */
    window.genSignal = function (tid) {
        if (!socket) { alert('Socket 未连接'); return; }
        // 读卡片日期框（与「重新生成基准图」共用同一组输入 → 图与信号同窗口、同源）
        const card = document.querySelector('.quant-card[data-tid="' + tid + '"]');
        const start = card?.querySelector('input[name="start"]')?.value || '';
        const end   = card?.querySelector('input[name="end"]')?.value   || '';
        // 进入 loading 态
        setBadge(tid, 'loading', '评估中');
        setStatus(tid, '正在取数 + 计算信号 + 画图...');
        _inflight++;
        window.__alphagptBusySet && window.__alphagptBusySet('quant', _inflight > 0);
        socket.emit('generate_signal', { tracking_id: tid, start, end });
    };

    /* ── 信号结果 ── */
    socket?.on('quant_signal', (d) => {
        const tid = d.tracking_id;
        if (tid === undefined) return;
        _inflight = Math.max(0, _inflight - 1);
        window.__alphagptBusySet && window.__alphagptBusySet('quant', _inflight > 0);
        if (d.error) {
            const errMap = { training_running: '训练进行中，请稍后再生成信号',
                             already_running: '该公式正在评估中，请稍候',
                             formula_locked: '该公式已锁定（公式库已满），删除多余公式后解锁；需要更大容量可联系开发者定制扩展' };
            setBadge(tid, 'idle', '待测');
            setStatus(tid, errMap[d.error] || ('❌ ' + d.error));
            return;
        }
        const sig = d.signal;
        const DIRECTIONAL = window.__signalDirectional === true;   // 圆圈中心：True=方向文字(▲看多/▬观望)，False=纯因子末值(合规)
        const caption = DIRECTIONAL ? (sig === 'buy' ? '看多' : '观望')
            : (d.factor_value != null ? '因子值 ' + (+(d.factor_value)).toFixed(4) : '观望');  // 状态栏：数值模式显因子末值(4位)，研究措辞非做空
        const _fv = d.factor_value != null ? ' ' + (+(d.factor_value)).toFixed(2) : '';  // 因子末值(2位)，方向模式追加在方向文字后
        const label = DIRECTIONAL ? (sig === 'buy' ? '▲ 看多' : '▬ 观望') + _fv
            : (d.factor_value != null ? (+(d.factor_value)).toFixed(2) : '待测');  // 圆圈中心：方向模式=方向文字+因子末值；数值模式=纯数值
        setBadge(tid, sig, label);
        setFreshness(tid, d.fresh, d.basis_date);          // 新鲜度徽标 + stale 置灰
        setArcFormula(tid, d.confidence, d.signal);                   // 左弧=公式强度 + 图例数值（看多红/观望灰）
        setArcAi(tid, d.deepseek);                          // 右弧=AI评分 + 图例数值
        setResonance(tid, sig, d.deepseek);                 // 共振标（公式因子 vs AI 综合评分）
        setHitRate(tid, d.hit_rate);                        // 近 20 日命中率（真·准确度）
        const freshNote = d.fresh === false ? ' · ⚠️数据陈旧' : '';
        setStatus(tid, `${d.last_date || ''} ${caption}${freshNote} · ${d.duration || ''}s`);
        // AI 研究仪表盘(Phase 4 结构化渲染: 评分/趋势/参考区间/检查清单)
        if (d.deepseek != null) {
            const el = document.getElementById('ds-' + tid);
            if (el) {
                let r = (typeof d.deepseek === 'object') ? d.deepseek : null;
                if (!r && typeof d.deepseek === 'string') { try { r = JSON.parse(d.deepseek); } catch (e) { r = null; } }
                el.innerHTML = (r && typeof r === 'object') ? renderStockDecision(r) : (d.deepseek || '（无分析）');
            }
        }
        if (d.tavily != null) {
            const el = document.getElementById('tv-' + tid);
            if (el) el.innerHTML = renderNewsHtml(d.tavily.news);   // 标题可点击 → 新页打开原文
        }
        // 基准图原地刷新（png 文件名带 HHMMSS 每次唯一，浏览器必然重新拉取，无需 cache-bust）
        if (d.png) {
            const img = document.getElementById('chart-' + tid);
            if (img) img.src = '/reports-file?path=' + encodeURIComponent(d.png);
        }
        // 徽标下方日期：d.last_date 已是 YYYY-MM-DD，与模板 |fmt_date 逐字节一致，直接写入
        if (d.last_date) {
            const sc = document.getElementById('sigconf-' + tid);
            if (sc) sc.textContent = d.last_date;
        }
    });

    socket?.on('quant_log', (d) => {
        const tid = d.tracking_id;
        if (tid !== undefined) setStatus(tid, d.line);
    });

    function setBadge(tid, cls, text) {
        const el = document.getElementById('sig-' + tid);
        if (!el) return;
        // 数值模式(合规)追加 sig-numeric：中心文字用中性色，不随 buy/hold 红灰
        el.className = 'signal-badge ' + cls + (window.__signalDirectional ? '' : ' sig-numeric');   // 注意：重置 class，stale 由 setFreshness 在其后追加
        const t = el.querySelector('.sig-text');
        if (t && text) t.textContent = text;
    }
    function setStatus(tid, msg) {
        const el = document.getElementById('status-' + tid);
        if (el) el.textContent = msg;
    }

    /* ── 数据新鲜度（圆圈置灰 + 徽标）── */
    function _staleFlag(badge) {
        return badge.querySelector('.sig-stale-flag');
    }
    function setFreshness(tid, fresh, basisDate) {
        const freshEl = document.getElementById('fresh-' + tid);
        const badge = document.getElementById('sig-' + tid);
        if (fresh === true) {
            if (freshEl) { freshEl.className = 'fresh-badge fresh'; freshEl.textContent = '✅ 基于 ' + (basisDate || '今日') + ' 收盘'; }
            if (badge) { badge.classList.remove('stale'); const f = _staleFlag(badge); if (f) f.remove(); }
        } else if (fresh === false) {
            if (freshEl) { freshEl.className = 'fresh-badge stale'; freshEl.textContent = '⚠️ 数据仅到 ' + (basisDate || '上日'); }
            if (badge) {
                badge.classList.add('stale');
                if (!_staleFlag(badge)) {
                    const f = document.createElement('span');
                    f.className = 'sig-stale-flag'; f.textContent = '⚠️';
                    f.title = '当日数据未取得，信号基于上日收盘';
                    badge.appendChild(f);
                }
            }
        } else {
            if (freshEl) { freshEl.className = 'fresh-badge unknown'; freshEl.textContent = '新鲜度未知'; }
        }
    }
    /* ── 近 20 日命中率（真·准确度）── */
    function setHitRate(tid, hitRate) {
        const el = document.getElementById('hit-' + tid);
        if (!el) return;
        el.textContent = hitRate != null ? '🎯 近20日命中 ' + (hitRate * 100).toFixed(0) + '%' : '命中率待测';
    }
    /* ── 双半圆环填充：左弧=公式强度、右弧=AI评分，+ 数值图例。
       dasharray "0 4 f (96-f)"：首尾各留 4 单位缺口(顶/底极点断开)，f=分数×0.92(满档留缺口) ── */
    function _gapDash(pct0to100) {
        const f = Math.round(pct0to100 || 0);
        return `${f} ${100 - f}`;
    }
    function setArcFormula(tid, confidence, signal) {
        const arc = document.getElementById('arc-formula-' + tid);
        const leg = document.getElementById('leg-formula-' + tid);
        const pct = confidence != null ? Math.round(confidence * 100) : null;
        if (arc) arc.setAttribute('stroke-dasharray', _gapDash(pct != null ? pct : 0));
        if (leg) leg.textContent = pct != null ? pct : '—';
        // 左弧颜色随信号：看多红、观望/无 中灰（改 fg 渐变两 stop 的 stop-color）
        const fcolor = (signal === 'buy') ? '#ec4899' : 'var(--text-secondary,#94a3b8)';  // buy=粉红（柔化，非正红）、hold=灰
        document.getElementById('fg-' + tid)?.querySelectorAll('stop').forEach(s => { s.style.stopColor = fcolor; });
    }
    function setArcAi(tid, deepseek) {
        const arc = document.getElementById('arc-ai-' + tid);
        const leg = document.getElementById('leg-ai-' + tid);
        const score = (deepseek && typeof deepseek === 'object' && deepseek.sentiment_score != null)
            ? deepseek.sentiment_score : null;
        if (arc) arc.setAttribute('stroke-dasharray', _gapDash(score != null ? score : 0));
        if (leg) leg.textContent = score != null ? Math.round(score) : '—';
    }
    /* ── 共振标：公式因子 vs AI 综合评分。纯分数对称阈值（与 Jinja 初始渲染同规则，
       避免首屏与推送后结论不一致）。不用正则关键字——子串匹配会把含否定前缀的词误判。── */
    function setResonance(tid, signal, deepseek) {
        const el = document.getElementById('resonance-' + tid);
        if (!el) return;
        const hasAI = window.__quantHasDeepseek;
        const fDir = signal === 'buy' ? 'up' : 'down';   // only_long: buy=看多, hold=观望
        const score = (deepseek && typeof deepseek === 'object' && deepseek.sentiment_score != null)
            ? deepseek.sentiment_score : null;
        let cls, txt;
        if (!hasAI) { cls = 'neutral'; txt = '🚫 未启用 AI'; }
        else if (score == null) { cls = 'neutral'; txt = '➖ 待 AI 分析'; }
        else if ((score >= 60 && fDir === 'up') || (score <= 40 && fDir === 'down')) { cls = 'agree'; txt = '✅ 因子与综合面一致'; }
        else if (score >= 60 || score <= 40) { cls = 'conflict'; txt = '⚠️ 因子·综合面背离'; }
        else { cls = 'neutral'; txt = '➖ 部分重合'; }
        el.className = 'resonance ' + cls;
        el.textContent = txt;
    }

    /* ── 个股研究仪表盘结构化渲染(Phase 4 富渲染) ──
       r: AnalysisReportSchema 对象(LLM 返回,容忍缺字段)。全程研究口吻,无交易指令词。
       分区:头部综合 → 一句话倾向 → 信号标 → 数据视角 → 参考区间 → 研究配置 →
            信号归因 → 情报 → 展望 → 观察清单 → 分人群 → 研究理由 → 综述 → 免责底注。
       注:用 .ds-root 包裹以覆盖父级 .ai-body 的 white-space:pre-wrap,保证块级排版干净。 */
    function renderStockDecision(r) {
        if (!r || typeof r !== 'object') return '（无分析）';
        const dash = r.dashboard || {};
        const cc = dash.core_conclusion || {};
        const dp = dash.data_perspective || {};
        const intel = dash.intelligence || {};
        const bp = dash.battle_plan || {};
        const sp = bp.sniper_points || {};
        const ps = bp.position_strategy || {};
        const sa = dash.signal_attribution || {};
        const pa = cc.position_advice || {};
        const ts = dp.trend_status || {};
        const pp = dp.price_position || {};
        const va = dp.volume_analysis || {};
        const cs = dp.chip_structure || {};

        const has = v => v != null && v !== '' && !(Array.isArray(v) && v.length === 0);
        const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        const scoreCls = s => (typeof s !== 'number') ? 'neutral' : (s >= 60 ? 'up' : (s <= 40 ? 'down' : 'neutral'));
        const ADVISORY = window.__aiDashboardAdvisory === true;  // false=隐藏方向/买卖点/投资建议（降荐股定性），定制服务可放开

        let h = '<div class="ds-root">';

        // 头部:综合评分 / 趋势 / 倾向 / 把握度
        h += '<div class="ds-head">'
            + `<span class="ds-score ${ADVISORY ? scoreCls(r.sentiment_score) : 'neutral'}" title="AI综合均线/资金/舆情打分 0-100,含主观成分,仅研究参考">综合 ${r.sentiment_score != null ? r.sentiment_score : '—'}/100</span>`
            + (ADVISORY && has(r.trend_prediction) ? `<span class="ds-trend">📈 ${esc(r.trend_prediction)}</span>` : '')
            + (ADVISORY && has(r.operation_advice) ? `<span class="ds-advice">倾向：${esc(r.operation_advice)}</span>` : '')
            + (has(r.confidence_level) ? `<span class="ds-conf">把握：${esc(r.confidence_level)}</span>` : '')
            + '</div>';

        // 一句话研究倾向
        if (ADVISORY && has(cc.one_sentence)) h += `<div class="ds-conclusion">${esc(cc.one_sentence)}</div>`;

        // 信号标 + 时效（均 advisory，统一挂 ADVISORY 门；time_sensitivity 原未挂门，2026-08-01 修）
        if (ADVISORY && (has(cc.signal_type) || has(cc.time_sensitivity))) {
            h += '<div class="ds-signal-row">'
                + (has(cc.signal_type) ? `<span class="ds-sigtype">${esc(cc.signal_type)}</span>` : '')
                + (has(cc.time_sensitivity) ? `<span class="ds-when">⏱ ${esc(cc.time_sensitivity)}</span>` : '')
                + '</div>';
        }

        // 数据视角:趋势 / 价格 / 量能 / 筹码
        const dataRows = [];
        if (has(ts.ma_alignment)) {
            let t = `均线 ${esc(ts.ma_alignment)}`;
            if (has(ts.trend_score)) t += ` · 趋势分 ${esc(ts.trend_score)}`;
            dataRows.push(['趋势', t]);
        }
        if (has(pp.current_price) || has(pp.ma5) || has(pp.bias_ma5)) {
            let t = '';
            if (has(pp.current_price)) t += `现价 ${esc(pp.current_price)} `;
            if (has(pp.bias_ma5)) t += `· 乖离 ${esc(pp.bias_ma5)}(${esc(pp.bias_status || '—')}) `;
            if (has(pp.support_level)) t += `· 支撑 ${esc(pp.support_level)} `;
            if (has(pp.resistance_level)) t += `· 压力 ${esc(pp.resistance_level)}`;
            dataRows.push(['价格', t.trim()]);
        }
        if (has(va.volume_status)) {
            let t = `${esc(va.volume_status)}`;
            if (has(va.volume_ratio)) t += ` · 量比 ${esc(va.volume_ratio)}`;
            if (has(va.turnover_rate)) t += ` · 换手 ${esc(va.turnover_rate)}%`;
            dataRows.push(['量能', t]);
        }
        if (has(cs.chip_health)) {
            let t = `${esc(cs.chip_health)}`;
            if (has(cs.profit_ratio)) t += ` · 获利 ${esc(cs.profit_ratio)}%`;
            if (has(cs.concentration)) t += ` · 集中 ${esc(cs.concentration)}%`;
            dataRows.push(['筹码', t]);
        }
        if (dataRows.length) {
            h += '<div class="ds-section"><div class="ds-section-t">📊 数据视角</div>';
            dataRows.forEach(([k, v]) => { h += `<div class="ds-kvrow"><span class="ds-k">${k}</span><span class="ds-v">${v}</span></div>`; });
            h += '</div>';
        }

        // 参考区间(留意参与位 / 次级关注 / 下方风险 / 上方参考)
        if (ADVISORY && (has(sp.ideal_buy) || has(sp.secondary_buy) || has(sp.stop_loss) || has(sp.take_profit))) {
            h += '<div class="ds-section"><div class="ds-section-t">🎯 参考区间<span class="ds-section-hint">非买卖建议</span></div><div class="ds-sniper-grid">';
            if (has(sp.ideal_buy)) h += `<div class="ds-snipe-cell"><span class="ds-snipe-k">留意参与位</span><span class="ds-snipe-v">${esc(sp.ideal_buy)}</span></div>`;
            if (has(sp.secondary_buy)) h += `<div class="ds-snipe-cell"><span class="ds-snipe-k">次级关注位</span><span class="ds-snipe-v">${esc(sp.secondary_buy)}</span></div>`;
            if (has(sp.stop_loss)) h += `<div class="ds-snipe-cell risk"><span class="ds-snipe-k">下方风险位</span><span class="ds-snipe-v">${esc(sp.stop_loss)}</span></div>`;
            if (has(sp.take_profit)) h += `<div class="ds-snipe-cell bull"><span class="ds-snipe-k">上方参考位</span><span class="ds-snipe-v">${esc(sp.take_profit)}</span></div>`;
            h += '</div></div>';
        }

        // 研究配置
        if (ADVISORY && (has(ps.suggested_position) || has(ps.entry_plan) || has(ps.risk_control))) {
            h += '<div class="ds-section"><div class="ds-section-t">📐 研究配置<span class="ds-section-hint">研究参考</span></div><div class="ds-kvcol">';
            if (has(ps.suggested_position)) h += `<div class="ds-kvrow"><span class="ds-k">思路</span><span class="ds-v">${esc(ps.suggested_position)}</span></div>`;
            if (has(ps.entry_plan)) h += `<div class="ds-kvrow"><span class="ds-k">留意节奏</span><span class="ds-v">${esc(ps.entry_plan)}</span></div>`;
            if (has(ps.risk_control)) h += `<div class="ds-kvrow"><span class="ds-k">风控</span><span class="ds-v">${esc(ps.risk_control)}</span></div>`;
            h += '</div></div>';
        }

        // 信号归因(技术/新闻/基本面/大盘 占比 + 最强多空信号)
        const tech = sa.technical_indicators, news2 = sa.news_sentiment, funda = sa.fundamentals, mkt = sa.market_conditions;
        if (has(tech) || has(news2) || has(funda) || has(mkt)) {
            h += '<div class="ds-section"><div class="ds-section-t">🧠 信号归因</div><div class="ds-attrib">';
            const attrRow = (k, v) => has(v)
                ? `<div class="ds-attr"><span class="ds-attr-k">${k}</span><span class="ds-attr-bar"><i style="width:${Math.max(0, Math.min(100, Number(v) || 0))}%"></i></span><span class="ds-attr-v">${esc(v)}%</span></div>`
                : '';
            h += attrRow('技术', tech) + attrRow('新闻', news2) + attrRow('基本面', funda) + attrRow('大盘', mkt);
            h += '</div>';
            if (ADVISORY && has(sa.strongest_bullish_signal)) h += `<div class="ds-bull">🔺 最强看多：${esc(sa.strongest_bullish_signal)}</div>`;
            if (ADVISORY && has(sa.strongest_bearish_signal)) h += `<div class="ds-bear">🔻 最强看空：${esc(sa.strongest_bearish_signal)}</div>`;
            h += '</div>';
        }

        // 情报:利好 / 风险 / 舆情
        const listBlock = arr => (Array.isArray(arr) && arr.length) ? `<ul>${arr.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
        if (has(intel.positive_catalysts) || has(intel.risk_alerts) || has(intel.sentiment_summary) || has(intel.latest_news)) {
            h += '<div class="ds-section"><div class="ds-section-t">📰 情报</div>';
            if (has(intel.positive_catalysts)) h += `<div class="ds-cat bull">🔴 利好</div>${listBlock(intel.positive_catalysts)}`;
            if (has(intel.risk_alerts)) h += `<div class="ds-cat risk">🚨 风险</div>${listBlock(intel.risk_alerts)}`;
            if (has(intel.sentiment_summary)) h += `<div class="ds-kvrow"><span class="ds-k">舆情</span><span class="ds-v">${esc(intel.sentiment_summary)}</span></div>`;
            if (has(intel.latest_news) && !has(intel.positive_catalysts) && !has(intel.risk_alerts)) h += `<div class="ds-kvrow"><span class="ds-k">动态</span><span class="ds-v">${esc(intel.latest_news)}</span></div>`;
            h += '</div>';
        }

        // 展望
        if (ADVISORY && (has(r.short_term_outlook) || has(r.medium_term_outlook))) {
            h += '<div class="ds-section"><div class="ds-section-t">🔭 展望</div><div class="ds-kvcol">';
            if (has(r.short_term_outlook)) h += `<div class="ds-kvrow"><span class="ds-k">短期</span><span class="ds-v">${esc(r.short_term_outlook)}</span></div>`;
            if (has(r.medium_term_outlook)) h += `<div class="ds-kvrow"><span class="ds-k">中期</span><span class="ds-v">${esc(r.medium_term_outlook)}</span></div>`;
            h += '</div></div>';
        }

        // 观察清单(原 action_checklist)
        if (Array.isArray(bp.action_checklist) && bp.action_checklist.length) {
            h += '<div class="ds-section"><div class="ds-section-t">✅ 观察清单</div><div class="ds-checklist">'
                + bp.action_checklist.map(x => `<span class="ds-chip">${esc(x)}</span>`).join('')
                + '</div></div>';
        }

        // 分人群参考(未参与 / 已参与)
        if (ADVISORY && (has(pa.no_position) || has(pa.has_position))) {
            h += '<div class="ds-section"><div class="ds-section-t">👥 分人群参考</div><div class="ds-kvcol">';
            if (has(pa.no_position)) h += `<div class="ds-kvrow"><span class="ds-k">未参与</span><span class="ds-v">${esc(pa.no_position)}</span></div>`;
            if (has(pa.has_position)) h += `<div class="ds-kvrow"><span class="ds-k">已参与</span><span class="ds-v">${esc(pa.has_position)}</span></div>`;
            h += '</div></div>';
        }

        // 研究理由
        if (ADVISORY && has(r.buy_reason)) h += `<div class="ds-reason"><span class="ds-reason-k">📝 研究理由</span>${esc(r.buy_reason)}</div>`;

        // 综述
        if (has(r.analysis_summary)) h += `<div class="ds-summary">${esc(r.analysis_summary)}</div>`;

        // 免责底注(常驻)
        h += '<div class="ds-disclaimer">ℹ️ 以上为 AI 模型基于公开数据的算法回显,仅供量化研究学习,<b>不构成任何投资建议或买卖指令</b>。市场有风险,决策请独立判断。</div>';

        h += '</div>';
        return h;
    }

    /* ── QMT 弹窗 ── */
    window.openQmt = function (tid) {
        const f = document.getElementById('qmtForm');
        f.action = '/quant/' + tid + '/qmt';
        document.getElementById('qmtModal').classList.add('show');
    };
    window.closeQmt = function () { document.getElementById('qmtModal').classList.remove('show'); };

    /* ── 报告弹窗（回测报告 / 信号日志，共用一个 modal）── */
    const REPORT_TITLES = {
        reality: '📊 样本外回测报告 Final Reality Check',
        signal: '📋 AlphaGPT-Evo 次日信号日志',
    };
    window.openReport = function (tid, kind) {
        document.getElementById('reportTitle').textContent = REPORT_TITLES[kind] || '报告';
        const body = document.getElementById('reportBody');
        body.innerHTML = '<div class="empty-state">加载中…</div>';
        document.getElementById('reportModal').classList.add('show');
        fetch('/reports-file?path=qt' + tid + '_' + kind + '.html')
            .then(r => r.ok ? r.text() : Promise.reject(r.status))
            .then(html => { body.innerHTML = html; })
            .catch(() => { body.innerHTML = '<div class="empty-state">暂无报告，请先点「生成信号」或「重新生成基准图」</div>'; });
    };
    window.closeReport = function () { document.getElementById('reportModal').classList.remove('show'); };
    document.querySelectorAll('.modal-mask').forEach(m =>
        m.addEventListener('click', e => { if (e.target === m) m.classList.remove('show'); }));

    /* ── 重新生成基准图：同步 POST 前显示 loading 浮层（约 5~15s，防用户以为卡死而刷新/离开）── */
    document.querySelectorAll('form.regen-inline').forEach(form => {
        form.addEventListener('submit', function () {
            const btn = this.querySelector('button[type=submit]');
            if (btn) btn.disabled = true;   // 防重复点击
            const ov = document.getElementById('regenOverlay');
            if (ov) ov.classList.add('show');
            // 不 preventDefault：让 POST 正常提交，服务端 redirect 回来本页卸载、浮层自然消失
        });
    });
    // bfcache 兜底：浏览器前进/后退恢复缓存的「提交中」页面时撤掉浮层（防残留卡死）
    window.addEventListener('pageshow', e => {
        if (e.persisted) document.getElementById('regenOverlay')?.classList.remove('show');
    });

    /* ── 首屏富渲染：ds-/tv- 卡的历史落库数据（刷新/非交易日打开也完整显示，不依赖实时 emit）──
       修「非交易日复盘 AI 卡/近期动态/圆圈AI弧空白」：之前 tv 卡写死占位不读字段、ds 卡只存 summary 文本。
       现落库完整 JSON，首屏用 renderStockDecision 富渲染 / 新闻列表渲染。兼容旧 summary 文本（非 JSON 直接显示）。 */
    document.querySelectorAll('.ai-body[data-ds]').forEach(el => {
        const raw = el.dataset.ds;
        if (!raw) return;
        try {
            const r = JSON.parse(raw);
            if (r && typeof r === 'object') { el.innerHTML = renderStockDecision(r); return; }
            el.textContent = raw;
        } catch (e) { el.textContent = raw; }   // 旧 summary 文本（非合法 JSON）直接显示，兼容历史记录
    });
    document.querySelectorAll('.ai-body[data-tv]').forEach(el => {
        try {
            const news = JSON.parse(el.dataset.tv || '');
            if (Array.isArray(news)) { el.innerHTML = renderNewsHtml(news); return; }
        } catch (e) { /* 旧 label 文本非 JSON，落入兜底 */ }
        el.textContent = '近期动态：点击「生成信号」刷新';   // 旧记录/非数组兜底
    });
})();
