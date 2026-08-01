/* ============================================================================
   train.js —— 训练系统页 SocketIO 与交互
   依赖 base.js 已建立 window.AlphaGPT.socket（全局 SocketIO 连接）
   ============================================================================ */
(function () {
    const $ = (id) => document.getElementById(id);
    const socket = window.AlphaGPT?.socket;
    if (!socket) { console.warn('[train] socket 未就绪'); }

    const pbar = $('progressBar'), pLabel = $('progressLabel'), pPct = $('progressPercent');
    const sDot = $('statusDot'), sText = $('statusText');
    const btnStart = $('btnStart'), btnStop = $('btnStop');
    const logBox = $('logContainer'), logCount = $('logCount');
    const resultCard = $('resultCard'), resultBadge = $('resultBadge'), resultMsg = $('resultMsg');
    const formulaText = $('bestFormulaText');
    let logLines = 0;
    let _lastPct = 0;

    /* ── 硬件监控（从顶栏挪到训练页；进入拉一次，训练中每 3s 周期刷新）── */
    const warning = $('trainWarning');
    let _hwTimer = null;
    const startHwPoll = () => { if (_hwTimer) return; socket?.emit('request_hw_stats'); _hwTimer = setInterval(() => socket?.emit('request_hw_stats'), 3000); };
    const stopHwPoll = () => { if (_hwTimer) { clearInterval(_hwTimer); _hwTimer = null; } };
    socket?.on('hw_stats', (data) => {
        if (!data || data.error) {
            ['hwCpu','hwRam','hwGpu','hwVRam'].forEach(id => { const el = $(id); if (el) el.textContent = '--'; });
            return;
        }
        const set = (id, v) => { const el = $(id); if (el) el.textContent = (v != null ? v : '--'); };
        set('hwCpu', data.cpu_percent != null ? data.cpu_percent.toFixed(1) : '--');
        set('hwRam', data.ram_percent != null ? data.ram_percent.toFixed(1) : '--');
        if (data.gpus && data.gpus.length > 0) {
            const g = data.gpus.find(x => x.load_pct !== null || (x.name && !x.name.includes('Virtual'))) || data.gpus[data.gpus.length - 1];
            set('hwGpu', g.load_pct != null ? g.load_pct.toFixed(1) : '--');
            if (g.mem_percent != null) set('hwVRam', g.mem_percent.toFixed(1));
            else if (g.mem_used_mb != null && g.mem_total_mb != null) set('hwVRam', (g.mem_used_mb / g.mem_total_mb * 100).toFixed(1));
            else set('hwVRam', '--');
        } else { set('hwGpu', '--'); set('hwVRam', '--'); }
    });
    socket?.emit('request_hw_stats');   // 进入训练页拉一次

    /* ── 收集表单参数 ── */
    function collectParams() {
        const p = {};
        document.querySelectorAll('[data-p]').forEach(el => {
            if (el.disabled || el.closest('fieldset[disabled]')) return;  // 跳过锁定的禁用输入（高级/微调参数默认锁定，按默认值训练）
            const key = el.dataset.p;
            if (el.type === 'checkbox') p[key] = el.checked;
            else if (el.tagName === 'SELECT') p[key] = el.value;
            else p[key] = el.value;
        });
        // 种子
        const seedMode = $('seedMode')?.value || 'A';
        p.seed_mode = seedMode;
        if (seedMode === 'D') p.manual_seed = $('manualSeed')?.value || 42;
        return p;
    }

    /* ── 开始训练 ── */
    window.startTraining = function () {
        if (!socket) return;
        const params = collectParams();
        // 标的代码必填：输入框默认留空，空则不发送——避免引擎回退隐式 601963 默认
        const code = (params.index_code || '').trim();
        if (!code) {
            const inp = document.querySelector('[data-p="index_code"]');
            if (inp) inp.focus();
            sDot.className = 'status-dot error';
            sText.textContent = '请填写标的代码';
            addLog('⚠️ 请先填写「标的代码」再开始训练（输入框默认留空）。', 'error');
            return;
        }
        params.index_code = code;
        clearLog();
        resultCard.classList.remove('show');
        $('bestSection')?.classList.add('hidden');
        pbar.style.width = '0%'; pbar.textContent = '0%'; pPct.textContent = '0%';
        _lastPct = 0;
        addLog('发送训练请求...', 'highlight');
        addLog(`标的 ${params.index_code} | 迭代 ${params.train_iterations} | Batch ${params.batch_size} | 寻优 ${params.auto_optimize_runs} 轮`, 'info');
        socket.emit('start_training', params);
    };

    window.stopTraining = function () { socket?.emit('stop_training'); };
    window.clearLog = function () {
        logBox.innerHTML = ''; logLines = 0; logCount.textContent = '0 条';
    };

    /* ── 保存到公式库 ── */
    window.saveFormula = function (fid) {
        if (!fid) return;
        if (!confirm('确认保存此公式到公式库？')) return;
        socket.emit('save_formula', { formula_id: fid });
        const btn = document.querySelector(`[data-save="${fid}"]`);
        if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }
    };

    function addLog(text, type) {
        type = type || '';
        const div = document.createElement('div');
        div.className = 'log-line ' + type;
        div.textContent = text;
        logBox.appendChild(div);
        logLines++; logCount.textContent = logLines + ' 条';
        logBox.scrollTop = logBox.scrollHeight;
    }

    /* ── 事件：进度 ── */
    socket?.on('train_progress', (d) => {
        const pct = d.percent || 0; _lastPct = pct;
        pbar.style.width = pct + '%'; pbar.textContent = pct.toFixed(1) + '%'; pPct.textContent = pct.toFixed(1) + '%';
        pLabel.textContent = `${d.desc}: ${d.epoch}/${d.total}${d.postfix ? ' | ' + d.postfix : ''}`;
        pbar.className = 'progress-bar';
        if (pct >= 100) pbar.classList.add('pct-done');
        else if (pct >= 66) pbar.classList.add('pct-late');
        else if (pct >= 33) pbar.classList.add('pct-mid');
        else pbar.classList.add('pct-early');
        pct < 100 ? pbar.classList.add('active-animate') : pbar.classList.remove('active-animate');
    });

    /* ── 事件：日志 ── */
    socket?.on('train_log', (d) => addLog(d.line));

    /* ── 事件：状态 ── */
    socket?.on('train_status', (d) => {
        const st = d.status;
        sDot.className = 'status-dot ' + st;
        sText.textContent = d.message || st;
        if (st === 'running' || st === 'starting') {
            btnStart.disabled = true; btnStop.disabled = false;
            pLabel.innerHTML = '训练进行中 <span class="train-spinner">⟳</span> 注意散热';
            pbar.classList.add('active-animate');
        } else if (st === 'completed') {
            btnStart.disabled = false; btnStop.disabled = true;
            pLabel.textContent = '训练完成！';
            addLog('训练完成！', 'highlight');
            if (_lastPct < 100) { pbar.style.width = '100%'; pbar.textContent = '100%'; pPct.textContent = '100%'; pbar.className = 'progress-bar pct-done'; }
            pbar.classList.remove('active-animate');
        } else if (st === 'error') {
            btnStart.disabled = false; btnStop.disabled = true;
            addLog(d.message, 'error'); pbar.classList.remove('active-animate');
        } else if (st === 'stopped') {
            btnStart.disabled = false; btnStop.disabled = true;
            addLog(d.message, 'warning'); pbar.classList.remove('active-animate');
        } else if (st === 'idle') {
            btnStart.disabled = false; btnStop.disabled = true; pLabel.textContent = '等待开始...';
        }
        // 警示框显示 + 硬件周期轮询：仅 running/starting 启用（静态醒目，不再红闪）
        const _active = (st === 'running' || st === 'starting');
        if (warning) { warning.style.display = _active ? 'flex' : 'none'; }
        if (_active) startHwPoll(); else stopHwPoll();
        if (d.seed) $('seedDisplay').textContent = d.seed;
    });

    /* ── 事件：最佳公式 ── */
    socket?.on('best_formula', (d) => {
        $('bestSection')?.classList.remove('hidden');
        if (d.ai_name) $('bestName').textContent = d.ai_name;
        if (d.formula) { formulaText.textContent = d.formula; addLog('最佳公式: ' + d.formula, 'highlight'); }
    });

    /* ── 事件：结果分类 ── */
    socket?.on('train_classified', (d) => {
        resultCard.classList.add('show');
        const tierMap = { bad: '劣质', normal: '普通', premium: '优质', unknown: '未知' };
        resultBadge.className = 'result-badge ' + (d.tier === 'unknown' ? 'normal' : d.tier);
        resultBadge.textContent = `${tierMap[d.tier] || d.label} · 夏普 ${d.test_sharpe}`;
        resultMsg.textContent = d.msg;
        $('resStock').textContent = (d.stock_code || '') + (d.ai_name ? ' · ' + d.ai_name : '');
        $('resFormula').textContent = d.formula_str || '';
        // 指标
        const setM = (id, v, isPct) => { const el = $(id); if (!el) return; el.textContent = (v == null ? '--' : (isPct ? (v * 100).toFixed(2) + '%' : Number(v).toFixed(3))); };
        setM('mAnnRet', d.metrics?.ann_ret, true);
        setM('mMaxDd', d.metrics?.max_dd, true);
        setM('mWinRate', d.metrics?.win_rate, true);
        setM('mCalmar', d.metrics?.calmar, false);
        // 基准图
        const img = $('resChart');
        if (d.png_url) { img.src = d.png_url; img.style.display = 'inline-block'; }
        else { img.style.display = 'none'; }
        // 保存按钮
        const saveBtn = $('btnSave');
        saveBtn.dataset.save = d.formula_id;
        saveBtn.onclick = () => window.saveFormula(d.formula_id);
        if (d.tier === 'bad') { saveBtn.disabled = true; saveBtn.textContent = '劣质不建议保存'; }
        else { saveBtn.disabled = false; saveBtn.textContent = '保存到公式库'; }
    });

    /* ── 事件：保存结果 ── */
    socket?.on('train_saved', (d) => {
        if (!d.ok) { addLog('保存失败: ' + (d.detail || ''), 'error'); return; }
        const btn = $('btnSave');
        btn.disabled = true; btn.textContent = '✓ 已保存';
        addLog('已保存到公式库', 'success');
    });

    /* ── 参数折叠 ── */
    document.querySelectorAll('.pg-head').forEach(h => {
        h.addEventListener('click', () => h.parentElement.classList.toggle('collapsed'));
    });

    /* ── 种子策略联动：选 D 极致复现时右侧切为手动种子输入框，其余显示当前种子（只读） ── */
    const seedMode = $('seedMode'), manualSeed = $('manualSeed'),
          seedDisplay = $('seedDisplay'), seedRightLabel = $('seedRightLabel'),
          seedHint = $('seedHint');
    const SEED_DETAILS = {
        'A': '每次训练用不同种子（基于用户ID+标的+时间戳的哈希），最大化探索公式解空间的不同区域。同一标的每次结果不同，适合广泛搜索、不追求复现。推荐新用户默认使用。',
        'B': '种子 = base + step × 轮次序号，等差递增。每轮种子间距固定，可系统化覆盖一段种子区间。需要配合下方 grid_base / grid_step 参数（高级组）。适合批量扫描实验。',
        'C': '种子 = 斐波那契序列 × 黄金比例(0.618) + 用户偏移。数学结构有规律的探索方向，介于随机(A)和等差(B)之间，部分研究表明黄金分割在优化搜索中有统计优势。',
        'D': '手动指定固定种子。同一标的+同一参数+同一种子 = 完全相同的训练结果（可复现）。用途：①复现某次训练（日志里有种子号，填回去即可）②排查公式是真有信号还是运气好（换几个固定种子对比）。',
        'E': '系统自动递增分配种子（10000→10001→10002…），本地持久化记录进度，换标的也连续递增、绝不重复。适合系统化、长期、穷举式地扫描某标的的最优种子区间。',
        'F': '调用 DeepSeek 大语言模型生成种子（让 AI 基于先验知识选数字）。需在系统设置配置 DeepSeek API Key；未配置时自动回退为策略 A。适合想借助 AI 直觉探索非常规种子的用户。'
    };
    function syncSeed() {
        const isD = seedMode && seedMode.value === 'D';
        if (manualSeed) manualSeed.style.display = isD ? '' : 'none';
        if (seedDisplay) seedDisplay.style.display = isD ? 'none' : '';
        if (seedRightLabel) seedRightLabel.textContent = isD ? '手动种子' : '当前种子';
        if (seedHint && seedMode) seedHint.textContent = SEED_DETAILS[seedMode.value] || '';
    }
    seedMode?.addEventListener('change', syncSeed);
    syncSeed();

    /* ── 参数 ⓘ 提示：param-scroll 有 overflow 裁剪，base.css 的 .help-tip::after 会被切到不可读；
       改在 body 上渲染深色浮层（.tip-pop），按 "?" 位置定位并夹在视口内，悬停即显。
       观感与量化页 .help-tip 一致（深色底白字），但不受任何 overflow 容器影响。── */
    (function () {
        let pop = null;
        const show = (el) => {
            const txt = el.getAttribute('data-tip');
            if (!txt) return;
            if (!pop) { pop = document.createElement('div'); pop.className = 'tip-pop'; document.body.appendChild(pop); }
            pop.textContent = txt;
            const w = pop.offsetWidth || 250;          // visibility:hidden 仍具尺寸，可测宽
            const r = el.getBoundingClientRect();
            let left = r.left + r.width / 2 - w / 2;   // 居中于 "?"
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));   // 夹在视口内
            pop.style.left = left + 'px';
            pop.style.top = (r.bottom + 8) + 'px';
            pop.classList.add('show');
        };
        const hide = () => { if (pop) pop.classList.remove('show'); };
        document.querySelectorAll('.param-card .help-tip[data-tip]').forEach(el => {
            el.addEventListener('mouseenter', () => show(el));
            el.addEventListener('mouseleave', hide);
        });
    })();

    addLog('训练系统就绪。配置参数后点击「开始训练」。', 'info');
})();
