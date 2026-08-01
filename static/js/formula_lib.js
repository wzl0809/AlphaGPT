/* ============================================================================
   formula_lib.js —— 股票代码↔名称自动补全（可复用组件）
   用法：attachStockInput(inputEl) 自动绑定补全下拉
   ============================================================================ */

/**
 * 给一个 input 绑定股票代码/名称自动补全。
 * 输入代码显示名称，输入名称显示代码；选中后回填。
 */
function attachStockInput(input) {
    if (!input || input.dataset.stockBound) return;
    input.dataset.stockBound = '1';

    // 下拉容器
    const list = document.createElement('div');
    list.className = 'autocomplete-list';
    if (getComputedStyle(input.parentElement).position === 'static') {
        input.parentElement.style.position = 'relative';
    }
    input.parentElement.appendChild(list);

    let items = [];
    let activeIdx = -1;
    let debounce = null;

    async function fetchQ(q) {
        try {
            const r = await fetch('/common/stock-search?q=' + encodeURIComponent(q));
            if (!r.ok) return [];
            return await r.json();
        } catch (e) { return []; }
    }

    function render(q) {
        list.innerHTML = '';
        if (!items.length) { list.classList.remove('show'); return; }
        items.forEach((it, i) => {
            const div = document.createElement('div');
            div.className = 'autocomplete-item' + (i === activeIdx ? ' active' : '');
            div.innerHTML = `<span class="ac-code">${it.code}</span>`;
            div.onmousedown = (e) => { e.preventDefault(); select(i); };
            list.appendChild(div);
        });
        list.classList.add('show');
    }

    function select(i) {
        const it = items[i];
        if (!it) return;
        input.value = it.code;
        input.dispatchEvent(new CustomEvent('stockselect', { detail: it }));
        list.classList.remove('show');
    }

    input.addEventListener('input', () => {
        clearTimeout(debounce);
        const v = input.value.trim();
        if (!v) { items = []; list.classList.remove('show'); return; }
        debounce = setTimeout(async () => {
            items = await fetchQ(v);
            activeIdx = -1;
            render(v);
        }, 180);
    });

    input.addEventListener('keydown', (e) => {
        if (!list.classList.contains('show')) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, items.length - 1); render(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
        else if (e.key === 'Enter' && activeIdx >= 0) { e.preventDefault(); select(activeIdx); }
        else if (e.key === 'Escape') { list.classList.remove('show'); }
    });

    input.addEventListener('blur', () => setTimeout(() => list.classList.remove('show'), 150));

    // 初始值：若为代码且有名称，显示提示
    window.AlphaGPT = window.AlphaGPT || {};
}

// 暴露为全局可复用
window.attachStockInput = attachStockInput;

// 自动绑定所有带 .stock-input 类的输入
document.querySelectorAll('.stock-input').forEach(attachStockInput);

// 弹窗点击遮罩关闭
document.querySelectorAll('.modal-mask').forEach(m => {
    m.addEventListener('click', (e) => { if (e.target === m) m.classList.remove('show'); });
});
