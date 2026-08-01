/* ============================================================================
   home.js —— 首页：大盘仪表盘指针 + 「刷新数据」AJAX
   ============================================================================ */

// 仪表盘指针：按 score 连续旋转 -90°(0分,偏弱) .. +90°(100分,强势)
// 抽成函数：首次加载 与 AJAX 替换 #marketCards 后 都调用。
function initGauge() {
    const label = document.getElementById('gaugeLabel');
    const needle = document.getElementById('gaugeNeedle');
    if (!label || !needle) return;
    const score = parseFloat(label.dataset.score);
    if (isNaN(score)) return;
    const deg = (score - 50) * 1.8;  // 0→-90°, 50→0°(指上), 100→+90°
    needle.style.transform = '';      // 先复位，让替换后的指针从 0° 重新动画
    requestAnimationFrame(() => { needle.style.transform = `rotate(${deg}deg)`; });
}

const _REFRESH_BTN_HTML = '<i class="bi bi-arrow-clockwise"></i> 刷新数据';

// 「刷新数据」按钮（大盘情绪/评述卡 header 右上角）：AJAX 拉 /market-refresh，替换 #marketCards 后重转指针。
// 按钮在 _market.html（AJAX 替换区）内 → 用 class + 事件委托，替换后新按钮仍工作。
function initMarketRefresh() {
    document.addEventListener('click', async (e) => {
        const btn = e.target.closest('.market-refresh');
        if (!btn || btn.disabled) return;
        const cards = document.getElementById('marketCards');
        if (!cards) return;
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-arrow-clockwise spin"></i> 刷新中…';
        try {
            const r = await fetch('/market-refresh');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            cards.innerHTML = await r.text();
            initGauge();                    // 新 DOM 的指针重转
            const ts = document.getElementById('marketTs');   // 替换后重新获取（marketTs 在 _market 内，会被替换）
            if (ts) {
                ts.style.color = '';        // 清掉之前可能的报错红
                const d = new Date();
                const p = n => String(n).padStart(2, '0');
                ts.textContent = '更新于 ' + p(d.getMonth() + 1) + '-' + p(d.getDate())
                                 + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
            }
        } catch (err) {
            const ts = document.getElementById('marketTs');
            if (ts) { ts.textContent = '刷新失败，可重试'; ts.style.color = 'var(--accent-red, #e5484d)'; }
        } finally {
            btn.disabled = false;
            btn.innerHTML = _REFRESH_BTN_HTML;
        }
    });
}

(function () {
    initGauge();
    initMarketRefresh();
    // 系统通知（公告）静态列表显示，不再轮播
})();
