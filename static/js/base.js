/* ============================================================================
   AlphaGPT 客户端 base.js —— 主题 / SocketIO / 硬件监控 / 侧边栏
   ============================================================================ */

/* ── 暗亮主题切换（localStorage 记忆）── */
function toggleTheme() {
    const html = document.documentElement;
    const icon = document.getElementById('themeIcon');
    if (html.getAttribute('data-theme') === 'dark') {
        html.removeAttribute('data-theme');
        if (icon) icon.className = 'bi bi-moon-stars';
        localStorage.setItem('alphagpt-theme', 'light');
    } else {
        html.setAttribute('data-theme', 'dark');
        if (icon) icon.className = 'bi bi-sun';
        localStorage.setItem('alphagpt-theme', 'dark');
    }
}

(function initTheme() {
    const saved = localStorage.getItem('alphagpt-theme');
    const icon = document.getElementById('themeIcon');
    if (saved === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        if (icon) icon.className = 'bi bi-sun';
    }
})();

/* ── 全屏（F 键切换）── */
function toggleFullscreen() {
    const d = document.documentElement;
    if (!document.fullscreenElement) d.requestFullscreen?.();
    else document.exitFullscreen?.();
}
document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;   // 输入框内 F 正常打字
    if (e.key.toLowerCase() === 'f') { e.preventDefault(); toggleFullscreen(); }
});

/* ── 侧边栏：滑动箭头 + 顶栏汉堡统一控制（桌面折叠/展开 · 手机抽屉）── */
const sb = document.getElementById('sidebar');
const sbArrow = document.getElementById('sidebarArrow');
const sbBackdrop = document.getElementById('sidebarBackdrop');
const sbToggleBtn = document.getElementById('sidebarToggle');

function isMobile() { return window.innerWidth <= 768; }

function syncSidebar() {
    if (!sb || !sbArrow) return;
    const mobile = isMobile();
    const collapsed = sb.classList.contains('collapsed');
    const open = sb.classList.contains('show');
    const icon = sbArrow.querySelector('i');
    if (mobile) {
        document.body.classList.remove('sb-collapsed');      // 手机不用折叠态定位
        document.body.classList.toggle('sb-open', open);
        if (icon) icon.className = 'bi ' + (open ? 'bi-chevron-left' : 'bi-list');
    } else {
        sb.classList.remove('show');                          // 桌面关抽屉
        document.body.classList.remove('sb-open');
        document.body.classList.toggle('sb-collapsed', collapsed);
        if (icon) icon.className = 'bi ' + (collapsed ? 'bi-chevron-right' : 'bi-chevron-left');
    }
}

function toggleSidebar() {
    if (!sb) return;
    if (isMobile()) {
        sb.classList.toggle('show');
    } else {
        const c = sb.classList.toggle('collapsed');
        try { localStorage.setItem('alphagpt-sb-collapsed', c ? '1' : '0'); } catch (e) {}
    }
    syncSidebar();
}

sbArrow?.addEventListener('click', toggleSidebar);
sbToggleBtn?.addEventListener('click', toggleSidebar);
sbBackdrop?.addEventListener('click', () => {
    sb.classList.remove('show'); syncSidebar();
});
window.addEventListener('resize', syncSidebar);
// 手机点菜单项后自动收起
document.querySelectorAll('.sidebar .nav-item').forEach(a => a.addEventListener('click', () => {
    if (isMobile() && sb.classList.contains('show')) { sb.classList.remove('show'); syncSidebar(); }
}));
// 恢复桌面折叠态
try {
    if (!isMobile() && localStorage.getItem('alphagpt-sb-collapsed') === '1') {
        sb.classList.add('collapsed');
    }
} catch (e) {}
syncSidebar();

/* ── SocketIO 连接 ── */
const connStatus = document.getElementById('connStatus');
const connWrap = document.getElementById('connWrap');
let socket = null;
try {
    // ⚠️ 只用 polling：async_mode='threading' + Werkzeug 开发服务器下，websocket 升级会触发
// `write() before start_response`（Werkzeug WSGI 与 engineio/simple-websocket 的 socket 接管不兼容），
// 浏览器每次切页/保存后刷新都会 500，并在重连风暴中把进程拖崩（系统设置保存即闪退即此因）。
// threading 模式官方仅支持长轮询；本地单机对延迟不敏感，polling 完全够用。
// 如需 websocket，须改 async_mode='eventlet'/'gevent' 并用生产级 WSGI。
socket = io({ transports: ['polling'], upgrade: false, reconnection: true, reconnectionAttempts: 30, reconnectionDelay: 1000 });
} catch (e) {
    if (connStatus) { connStatus.textContent = 'Socket 未加载'; }
}

if (socket) {
    socket.on('connect', () => {
        if (connStatus) { connStatus.textContent = '已连接'; }
        connWrap?.classList.add('online'); connWrap?.classList.remove('offline');
    });
    socket.on('disconnect', () => {
        if (connStatus) { connStatus.textContent = window.__serverStopping ? '服务已停止' : '已断开·重连中'; }
        connWrap?.classList.add('offline'); connWrap?.classList.remove('online');
    });
    socket.on('connect_error', () => {
        if (connStatus) { connStatus.textContent = '连接失败'; }
        connWrap?.classList.add('offline');
    });

    /* ── 训练日志（P02 用，此处兜底显示）── */
    socket.on('train_log', (data) => {
        // 留给训练页 hook；其他页面忽略
    });

    /* ── 训练状态 → 全局 busy 标志（全站通用；驱动 beforeunload 防误关）── */
    socket.on('train_status', (d) => {
        const s = d && d.status;
        window.__alphagptBusySet && window.__alphagptBusySet('train', s === 'running' || s === 'starting');
    });

    /* ── 服务端主动退出（托盘"退出"）→ 友好提示 + 停止重连，避免无声掉线 ── */
    socket.on('server_shutdown', (d) => {
        const reasonMap = { 'idle-timeout': '客户端因长时间空闲已自动退出',
                            'tray-quit': '您已从托盘退出客户端',
                            'manual': '客户端正在退出' };
        const msg = (d && reasonMap[d.reason]) || '客户端正在退出';
        window.__serverStopping = true;
        if (connStatus) { connStatus.textContent = '服务已停止'; }
        connWrap?.classList.add('offline'); connWrap?.classList.remove('online');
        try { socket.io.opts.reconnection = false; socket.disconnect(); } catch (e) {}
        __showShutdownBanner(msg);
    });
}

/* ── 任务进行中防误关：关/刷页面时弹原生确认（前端提醒；后端 busy 守卫才是底线）── */
window.__alphagpt_busy = { train: false, quant: false };
window.__alphagptBusySet = function (kind, v) {
    if (kind in window.__alphagpt_busy) window.__alphagpt_busy[kind] = !!v;
};
window.addEventListener('beforeunload', function (e) {
    if (window.__alphagpt_busy.train || window.__alphagpt_busy.quant) {
        e.preventDefault();   // 现代浏览器忽略自定义文案，但会弹原生"离开站点?"确认
        e.returnValue = '';   // 旧规范要求 returnValue 非空才弹
        return '';
    }
});

/* ── 服务端退出横幅（自包含样式，不依赖外部 CSS）── */
function __showShutdownBanner(msg) {
    if (document.getElementById('__shutdownBanner')) return;
    const b = document.createElement('div');
    b.id = '__shutdownBanner';
    b.innerHTML = '<i class="bi bi-power"></i>&nbsp; <b>' + msg + '</b>'
        + '<div style="font-size:.82rem;opacity:.85;margin-top:.35rem">'
        + '连接已关闭。如需继续使用，请重新双击启动器（run.bat / start.vbs）。</div>';
    Object.assign(b.style, {
        position: 'fixed', top: '14px', left: '50%', transform: 'translateX(-50%)',
        zIndex: '2147483647', maxWidth: '92vw', boxSizing: 'border-box',
        background: 'rgba(37,99,235,.96)', color: '#fff',
        padding: '.8rem 1.1rem', borderRadius: '10px',
        boxShadow: '0 8px 30px rgba(0,0,0,.35)', fontSize: '.92rem',
        fontFamily: 'inherit', lineHeight: 1.45, textAlign: 'center'
    });
    document.body.appendChild(b);
}

/* ── 股票代码↔名称自动补全（占位，P03 实装）── */
window.AlphaGPT = { socket, lookupStockName: (code) => null };
