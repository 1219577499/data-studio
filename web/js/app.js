/* Data Studio 前端逻辑（原生 JS，无框架依赖） */
(function () {
    'use strict';

    // ============================================================ 状态
    const S = {
        sid: '',
        catalog: null,
        datasets: [],
        active: null,
        columns: [],
        profile: null,
        charts: [],
        cleanOps: [],
        sources: [],
        chartSeq: 0,
        cmpCtx: null,      // 最近一次对比的上下文，切主题时要按它重画漂移图
        intent: '',        // 当前选中的分析意图
        filters: [],                            // 明细面板的筛选条件
        browseSort: { column: '', ascending: true },
    };

    /* 明细筛选的条件列表，和清洗算子「筛选行」保持一致 */
    const FILTER_OPS = [
        ['=', '等于'], ['!=', '不等于'], ['>', '大于'], ['>=', '大于等于'],
        ['<', '小于'], ['<=', '小于等于'], ['between', '介于'],
        ['contains', '包含'], ['startswith', '开头为'], ['in', '属于'],
        ['is_null', '为空'], ['not_null', '不为空'],
    ];
    const NO_VALUE_OPS = ['is_null', 'not_null'];
    const opText = (op) => {
        const found = FILTER_OPS.find(x => x[0] === op);
        return found ? found[1] : op;
    };

    const SQL_TPL = [
        { label: '分组聚合 TOP N', sql: 'SELECT city, COUNT(*) AS cnt, ROUND(SUM(amount),2) AS gmv\nFROM ecommerce_orders\nGROUP BY city\nORDER BY gmv DESC\nLIMIT 20' },
        { label: '环比（窗口函数 LAG）', sql: 'WITH m AS (\n  SELECT ship_date, SUM(amount) AS gmv\n  FROM ecommerce_orders GROUP BY ship_date\n)\nSELECT ship_date, gmv,\n       LAG(gmv) OVER (ORDER BY ship_date) AS prev,\n       ROUND((gmv - LAG(gmv) OVER (ORDER BY ship_date))\n             / NULLIF(LAG(gmv) OVER (ORDER BY ship_date),0) * 100, 2) AS pct\nFROM m ORDER BY ship_date' },
        { label: '分组内排名（ROW_NUMBER）', sql: 'SELECT * FROM (\n  SELECT city, category, SUM(amount) gmv,\n         ROW_NUMBER() OVER (PARTITION BY city ORDER BY SUM(amount) DESC) rn\n  FROM ecommerce_orders GROUP BY city, category\n) t WHERE rn <= 3' },
        { label: '去重取最新一条', sql: 'SELECT * FROM (\n  SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_time DESC) rn\n  FROM ecommerce_orders\n) t WHERE rn = 1' },
        { label: '累计求和（累计 GMV）', sql: 'SELECT ship_date, SUM(amount) day_gmv,\n       SUM(SUM(amount)) OVER (ORDER BY ship_date) AS cum_gmv\nFROM ecommerce_orders GROUP BY ship_date ORDER BY ship_date' },
        { label: '查缺失分布', sql: 'SELECT COUNT(*) total,\n       SUM(CASE WHEN city IS NULL THEN 1 ELSE 0 END) city_null,\n       SUM(CASE WHEN user_age IS NULL THEN 1 ELSE 0 END) age_null\nFROM ecommerce_orders' },
        // 这条 SQL 里含单引号，用双引号包字符串，避免转义写错把整个脚本搞崩
        { label: 'CASE WHEN 分箱', sql: "SELECT CASE WHEN amount < 500 THEN '0-500'\n            WHEN amount < 2000 THEN '500-2000'\n            WHEN amount < 8000 THEN '2000-8000'\n            ELSE '8000+' END AS bucket,\n       COUNT(*) cnt, ROUND(AVG(amount),2) avg_amount\nFROM ecommerce_orders GROUP BY bucket ORDER BY cnt DESC" },
    ];

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // ============================================================ 主题
    const curTheme = () => document.documentElement.dataset.theme || 'light';
    // 发给后端的图表请求要带上主题，否则深色界面里画出来的图还是浅色配色
    const themeArg = () => ({ theme: curTheme() });

    function applyTheme(t, redraw) {
        document.documentElement.dataset.theme = t;
        try { localStorage.setItem('ds-theme', t); } catch (e) { /* 隐私模式下写不了，忽略 */ }
        const btn = $('#btnTheme');
        if (btn) {
            btn.textContent = t === 'dark' ? '☀' : '☾';
            btn.title = t === 'dark' ? '切换到浅色' : '切换到深色';
        }
        if (redraw) redrawAllCharts();
    }

    function toggleTheme() {
        applyTheme(curTheme() === 'dark' ? 'light' : 'dark', true);
    }

    /* 主题一变，已经画出来的图得重画：坐标轴、tooltip 的文字颜色是后端按主题
       写进 option 里的，不重画就会留下上一套主题的配色，看着很怪。 */
    function redrawAllCharts() {
        if (S.active) {
            renderCorrCharts();
            renderCmpChart();
        }
        S.charts.forEach(c => refreshChart(c));
    }

    // ============================================================ 基础工具
    function toast(msg, kind) {
        const el = document.createElement('div');
        el.className = 'toast' + (kind ? ' ' + kind : '');
        el.textContent = msg;
        $('#toasts').appendChild(el);
        setTimeout(() => {
            el.style.transition = 'opacity .3s'; el.style.opacity = '0';
            setTimeout(() => el.remove(), 320);
        }, kind === 'error' ? 5200 : 2800);
    }

    async function api(url, opts) {
        opts = opts || {};
        const headers = Object.assign({ 'X-Session-Id': S.sid }, opts.headers || {});
        let body = opts.body;
        if (body && !(body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            body = JSON.stringify(body);
        }
        const r = await fetch(url, Object.assign({}, opts, { headers, body }));
        let json;
        try { json = await r.json(); }
        catch (e) { throw new Error('服务端返回了非 JSON 内容（HTTP ' + r.status + '）'); }
        if (!r.ok) {
            throw new Error((json.detail && json.detail.message) || json.message || ('HTTP ' + r.status));
        }
        return json;
    }

    const fmtNum = (n) => (n == null || n === '' || isNaN(n)) ? '–'
        : Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    const fmtBytes = (b) => b == null ? '–'
        : b > 1048576 ? (b / 1048576).toFixed(1) + ' MB'
            : (b / 1024).toFixed(0) + ' KB';
    const fmtPct = (p) => p == null ? '–' : (p * 100).toFixed(1) + '%';
    const fmtSigned = (p) => p == null ? '–' : (p >= 0 ? '+' : '') + (p * 100).toFixed(1) + '%';

    /* ECharts option 里的 formatter 是函数，JSON 传过来会变成字符串，
       这里递归还原成真函数，否则 tooltip / label 会把源码当文本显示出来。 */
    function reviveOption(obj) {
        if (typeof obj === 'string') {
            const t = obj.trim();
            if (/^function\s*\(/.test(t) || /^\(?\w*\)?\s*=>/.test(t)) {
                try { return (new Function('return ' + t))(); } catch (e) { return obj; }
            }
            return obj;
        }
        if (Array.isArray(obj)) return obj.map(reviveOption);
        if (obj && typeof obj === 'object') {
            for (const k of Object.keys(obj)) obj[k] = reviveOption(obj[k]);
        }
        return obj;
    }

    function modal(title, contentHtml, buttons) {
        const host = $('#modalHost');
        host.innerHTML = `
            <div class="modal-mask">
              <div class="modal">
                <div class="modal-head"><h3>${esc(title)}</h3>
                  <button class="x-close" data-close>&times;</button></div>
                <div class="modal-body">${contentHtml}</div>
                <div class="modal-foot"></div>
              </div>
            </div>`;
        const mask = $('.modal-mask', host);
        mask.addEventListener('click', e => { if (e.target === mask) host.innerHTML = ''; });
        $('[data-close]', host).onclick = () => { host.innerHTML = ''; };
        const foot = $('.modal-foot', host);
        (buttons || []).forEach(b => {
            const btn = document.createElement('button');
            btn.className = 'btn' + (b.primary ? ' btn-primary' : '');
            btn.textContent = b.label;
            btn.onclick = () => b.onClick(mask);
            foot.appendChild(btn);
        });
        return mask;
    }
    const closeModal = () => { $('#modalHost').innerHTML = ''; };

    const _chartInst = new WeakMap();
    function drawOption(target, option) {
        const el = typeof target === 'string' ? $(target) : target;
        if (!el) return;
        if (!option) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
        let inst = _chartInst.get(el);
        if (inst) inst.dispose();
        el.innerHTML = '';
        inst = echarts.init(el, null, { renderer: 'canvas' });
        inst.setOption(option, true);
        _chartInst.set(el, inst);
        return inst;
    }

    // ============================================================ 会话
    async function initSession() {
        const r = await api('/api/session', { method: 'POST' });
        S.sid = r.data.sid;
        S.datasets = r.datasets || [];
        $('#sessionInfo').textContent = '会话 ' + S.sid + ' · 数据仅存于本机内存';
    }

    async function loadCatalog() {
        const r = await api('/api/catalog');
        S.catalog = r.data;
        renderSamples();
        renderOpSelect();
        renderScenarios();
        renderIntents();
        renderSqlTpl();
    }

    function renderSqlTpl() {
        $('#sqlTpl').innerHTML = '<option value="">— 常用模板 —</option>' +
            SQL_TPL.map((t, i) => `<option value="${i}">${esc(t.label)}</option>`).join('');
    }

    function renderSamples() {
        const host = $('#sampleList');
        host.innerHTML = '';
        Object.values(S.catalog.samples || {}).forEach(s => {
            const el = document.createElement('div');
            el.className = 'sample-item';
            el.innerHTML = `<div><div class="sample-name">${esc(s.key)}</div>
                            <div class="sample-desc">${esc(s.label)}</div></div>
                            <button class="btn btn-sm">载入</button>`;
            el.onclick = async () => {
                try {
                    await api('/api/sample/load', { method: 'POST', body: { sid: S.sid, key: s.key } });
                    toast('已载入 ' + s.key, 'ok');
                    await refreshDatasets(s.key);
                } catch (e) { toast(e.message, 'error'); }
            };
            host.appendChild(el);
        });
    }

    // ============================================================ 数据源管理
    async function loadSources() {
        try {
            const r = await api('/api/db/list');
            S.sources = r.data;
        } catch (e) { S.sources = []; }
        renderSources();
    }

    function renderSources() {
        const host = $('#sourceList');
        host.innerHTML = '';
        if (!S.sources.length) {
            host.innerHTML = '<div class="empty">连过数据库后会出现在这里，下次直接用不用再连</div>';
            return;
        }
        S.sources.forEach(src => {
            const el = document.createElement('div');
            el.className = 'src-item';
            const offline = !!src.error;
            el.innerHTML = `
                <div class="src-head" data-toggle="${esc(src.cid)}">
                    <span class="src-dot ${offline ? 'off' : ''}"></span>
                    <span class="src-name">${esc(src.label)}</span>
                    <span class="src-badge">${src.tables ? src.tables.length : 0} 表</span>
                </div>
                <div class="src-body" data-body="${esc(src.cid)}" style="display:none"></div>`;
            host.appendChild(el);
            el.querySelector('[data-toggle]').onclick = () => toggleSource(src.cid);
        });
    }

    function toggleSource(cid) {
        const body = $(`[data-body="${cid}"]`);
        if (!body) return;
        if (body.style.display !== 'none') { body.style.display = 'none'; return; }
        body.style.display = '';
        const src = S.sources.find(s => s.cid === cid);
        if (!src) return;
        if (src.error) {
            body.innerHTML = `<div class="empty" style="color:var(--danger)">${esc(src.error)}
                <div style="margin-top:6px"><button class="btn btn-sm" data-retry="${cid}">重试</button></div></div>`;
            $(`[data-retry="${cid}"]`, body).onclick = async () => {
                try { await api('/api/db/' + cid + '/refresh', { method: 'POST' }); await loadSources(); }
                catch (e) { toast(e.message, 'error'); }
            };
            return;
        }
        const rows = (src.tables || []).map(t => `
            <div class="src-tbl">
                <span class="tname" data-prev="${cid}|${esc(t.name)}" title="点表名预览">${esc(t.name)}</span>
                <span class="tinfo">${t.rows != null ? fmtNum(t.rows) + ' 行' : t.type}</span>
                <button class="btn btn-sm" data-imp="${cid}|${esc(t.name)}">导入</button>
            </div>`).join('');
        body.innerHTML = `
            <div class="row" style="margin:6px 0">
                <button class="btn btn-sm" data-refresh="${cid}">刷新</button>
                <button class="btn btn-sm btn-danger" data-del="${cid}">断开</button>
            </div>
            <div class="limit-row">导入上限
                <select data-limit="${cid}" style="width:96px">
                    <option value="10000">1 万行</option>
                    <option value="100000" selected>10 万行</option>
                    <option value="500000">50 万行</option>
                </select>
            </div>
            <div class="src-tbls">${rows || '<div class="empty">没有表</div>'}</div>`;
        $(`[data-refresh="${cid}"]`, body).onclick = async () => {
            try { await api('/api/db/' + cid + '/refresh', { method: 'POST' }); await loadSources(); }
            catch (e) { toast(e.message, 'error'); }
        };
        $(`[data-del="${cid}"]`, body).onclick = async () => {
            try { await api('/api/db/' + cid, { method: 'DELETE' }); toast('已断开连接', 'ok'); await loadSources(); }
            catch (e) { toast(e.message, 'error'); }
        };
        $$('[data-imp]', body).forEach(b => b.onclick = async () => {
            const [c, t] = b.dataset.imp.split('|');
            await importTables(c, [t]);
        });
        $$('[data-prev]', body).forEach(sp => sp.onclick = async () => {
            const [c, t] = sp.dataset.prev.split('|');
            previewDbTable(c, t);
        });
    }

    async function importTables(cid, tables) {
        const sel = $(`[data-limit="${cid}"]`);
        const limit = sel ? parseInt(sel.value, 10) : 100000;
        try {
            const r = await api('/api/db/import', {
                method: 'POST', body: { sid: S.sid, cid, tables, limit },
            });
            toast('已导入 ' + r.data.map(x => x.name).join('、'), 'ok');
            if (r.errors && r.errors.length) toast(r.errors.map(e => e.table + ': ' + e.error).join('; '), 'error');
            await refreshDatasets(r.data[0] && r.data[0].name);
        } catch (e) { toast(e.message, 'error'); }
    }

    async function previewDbTable(cid, table) {
        try {
            const r = await api(`/api/db/${cid}/preview?table=${encodeURIComponent(table)}&n=20`);
            const d = r.data;
            const html = `<div class="hint">共 ${fmtNum(d.total)} 行，显示前 ${d.rows.length} 行</div>
                <div class="tbl-wrap" style="max-height:400px"><table class="tbl">
                <thead><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
                <tbody>${d.rows.map(r2 => `<tr>${r2.map(v =>
                `<td>${v === null || v === undefined ? '<span class="hint">NULL</span>' : esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>
                </table></div>`;
            modal(`预览 · ${table}`, html, [
                { label: '导入这张表', primary: true, onClick: () => { closeModal(); importTables(cid, [table]); } },
                { label: '关闭', onClick: closeModal },
            ]);
        } catch (e) { toast(e.message, 'error'); }
    }

    function dbModal() {
        const html = `
            <div class="form-row">
                <div class="field"><label>数据库类型</label>
                    <select id="dbType">
                        <option value="sqlite">SQLite 文件</option>
                        <option value="mysql">MySQL</option>
                        <option value="postgres">PostgreSQL</option>
                    </select>
                </div>
                <div class="field" id="dbPathWrap"><label>文件路径</label>
                    <div class="row"><input type="text" id="dbPath" placeholder="D:/data/mydb.sqlite">
                    <button class="btn btn-sm" id="dbPick">浏览</button></div></div>
                <div class="field" id="dbHostWrap" style="display:none"><label>主机</label>
                    <input type="text" id="dbHost" value="127.0.0.1"></div>
                <div class="field" id="dbPortWrap" style="display:none"><label>端口</label>
                    <input type="text" id="dbPort" value="3306"></div>
                <div class="field" id="dbUserWrap" style="display:none"><label>用户名</label>
                    <input type="text" id="dbUser" value="root"></div>
                <div class="field" id="dbPwdWrap" style="display:none"><label>密码</label>
                    <input type="text" id="dbPwd"></div>
                <div class="field" id="dbNameWrap" style="display:none"><label>数据库名</label>
                    <input type="text" id="dbName"></div>
            </div>
            <label class="form-inline hint" id="dbRememberWrap" style="display:none">
                <input type="checkbox" id="dbRemember" style="width:auto"> 记住密码（会明文存到本机 data/sources.json）
            </label>
            <div id="dbResult"></div>`;
        const m = modal('连接数据源', html, [
            { label: '连接', primary: true, onClick: connectDb },
            { label: '关闭', onClick: closeModal },
        ]);
        $('#dbType', m).onchange = () => {
            const isFile = $('#dbType', m).value === 'sqlite';
            [['dbPathWrap', isFile], ['dbHostWrap', !isFile], ['dbPortWrap', !isFile],
            ['dbUserWrap', !isFile], ['dbPwdWrap', !isFile], ['dbNameWrap', !isFile],
            ['dbRememberWrap', !isFile]]
                .forEach(([id, show]) => $(`#${id}`, m).style.display = show ? '' : 'none');
            $('#dbPort', m).value = $('#dbType', m).value === 'postgres' ? '5432' : '3306';
        };
        $('#dbPick', m).onclick = () => browseFiles('', (p) => { $('#dbPath', m).value = p; });
    }

    async function connectDb() {
        const dbtype = $('#dbType').value;
        const params = dbtype === 'sqlite'
            ? { path: $('#dbPath').value.trim() }
            : {
                host: $('#dbHost').value.trim(), port: parseInt($('#dbPort').value, 10) || 3306,
                user: $('#dbUser').value.trim(), password: $('#dbPwd').value,
                database: $('#dbName').value.trim(),
            };
        $('#dbResult').innerHTML = '<span class="loading"></span> 连接中…';
        try {
            const r = await api('/api/db/connect', {
                method: 'POST',
                body: { dbtype, params, remember_password: $('#dbRemember') ? $('#dbRemember').checked : false },
            });
            closeModal();
            toast('已连接：' + r.data.label, 'ok');
            await loadSources();
        } catch (e) {
            $('#dbResult').innerHTML = `<div class="empty" style="color:var(--danger)">${esc(e.message)}</div>`;
        }
    }

    // ============================================================ 数据集
    function renderDatasets() {
        const host = $('#dsList');
        host.innerHTML = '';
        $('#dsCount').textContent = S.datasets.length;
        if (!S.datasets.length) {
            host.innerHTML = '<div class="empty">还没有数据，先载入一份</div>';
        } else {
            S.datasets.forEach(d => {
                const el = document.createElement('div');
                el.className = 'ds-item' + (d.name === S.active ? ' active' : '');
                el.innerHTML = `
                    <div class="ds-name">${esc(d.name)}</div>
                    <div class="ds-meta">${fmtNum(d.rows)} 行 × ${d.cols} 列
                      ${d.parent ? ' · 派生自 ' + esc(d.parent) : ''}</div>
                    <button class="ds-del" title="移除">×</button>`;
                el.onclick = (ev) => {
                    if (ev.target.classList.contains('ds-del')) { removeDataset(d.name); return; }
                    selectDataset(d.name);
                };
                host.appendChild(el);
            });
        }
        // 对比面板的下拉
        ['#cmpLeft', '#cmpRight', '#cmpKey'].forEach(id => {
            const sel = $(id); if (!sel) return;
            const keep = sel.value;
            if (id === '#cmpKey') {
                sel.innerHTML = '<option value="">不指定（只比结构和分布）</option>' +
                    (S.columns || []).map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
            } else {
                sel.innerHTML = S.datasets.map(d =>
                    `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join('');
            }
            if (keep) sel.value = keep;
        });
        if ($('#cmpLeft') && S.datasets.length >= 2) {
            if (!$('#cmpLeft').value) $('#cmpLeft').value = S.datasets[0].name;
            if (!$('#cmpRight').value) $('#cmpRight').value = S.datasets[1].name;
        }
    }

    async function refreshDatasets(activeName) {
        const r = await api('/api/datasets?sid=' + encodeURIComponent(S.sid));
        S.datasets = r.data;
        if (activeName) S.active = activeName;
        if (S.active && !S.datasets.find(d => d.name === S.active)) S.active = null;
        const target = S.active || (S.datasets[0] && S.datasets[0].name);
        renderDatasets();
        if (target) await selectDataset(target);
        else renderDatasets();
    }

    async function removeDataset(name) {
        try {
            await api('/api/dataset/' + encodeURIComponent(name) + '?sid=' + S.sid, { method: 'DELETE' });
            S.datasets = S.datasets.filter(d => d.name !== name);
            if (S.active === name) {
                S.active = null;
                $('#ctxTitle').textContent = '未选择数据集';
                $('#ctxSub').textContent = '';
                $('#edaEmpty').style.display = '';
                $('#edaBody').style.display = 'none';
                S.charts = []; renderChartList();
            }
            renderDatasets();
            loadSchema();
        } catch (e) { toast(e.message, 'error'); }
    }

    async function selectDataset(name) {
        S.active = name;
        const d = S.datasets.find(x => x.name === name);
        $('#ctxTitle').textContent = name;
        $('#ctxSub').textContent = d ? `${fmtNum(d.rows)} 行 × ${d.cols} 列 · 来源 ${d.source || '未知'}` : '';
        $('#edaEmpty').style.display = 'none';
        $('#edaBody').style.display = '';
        // 切换数据集要清掉旧筛选条件 —— 列可能已经不一样了
        S.filters = [];
        S.browseSort = { column: '', ascending: true };
        // 列信息加载完再渲染，否则对比面板的关联键下拉还是上一份数据的列
        await Promise.all([loadColumns(), loadProfile(), loadSchema()]);
        renderFilterControls();
        renderFilterChips();
        await loadPreview();
        renderDatasets();
        ensureDefaultChart();
        resetCleanOps();
    }

    async function loadColumns() {
        if (!S.active) return;
        try {
            const r = await api('/api/dataset/' + encodeURIComponent(S.active) + '/columns?sid=' + S.sid);
            S.columns = r.data;
        } catch (e) { S.columns = []; }
    }

    const numCols = () => S.columns.filter(c => c.role === 'numeric');
    const dimCols = () => S.columns.filter(c => c.role !== 'numeric');

    // ============================================================ EDA
    async function loadProfile() {
        if (!S.active) return;
        const r = await api('/api/dataset/' + encodeURIComponent(S.active) + '/profile?sid=' + S.sid);
        S.profile = r.data;
        renderProfile();
        renderCorrCharts();
    }

    function renderProfile() {
        const p = S.profile, ov = p.overview;
        const kpis = [
            { l: '行数', v: Number(ov.rows).toLocaleString('zh-CN'), h: fmtBytes(ov.memory_bytes) + ' 内存' },
            { l: '列数', v: ov.cols, h: `${ov.numeric_cols} 数值 / ${ov.cat_cols} 类别 / ${ov.datetime_cols} 日期` },
            { l: '重复行', v: fmtNum(ov.duplicate_rows), h: fmtPct(ov.duplicate_pct), cls: ov.duplicate_rows ? 'warn' : '' },
            { l: '缺失单元格', v: fmtNum(ov.missing_cells), h: fmtPct(ov.missing_pct), cls: ov.missing_pct > .1 ? 'danger' : (ov.missing_pct ? 'warn' : '') },
            { l: '质量分', v: p.quality.score, h: p.quality.grade, cls: p.quality.score < 60 ? 'danger' : (p.quality.score < 80 ? 'warn' : '') },
            { l: '问题项', v: p.quality.issues.length, h: `严重 ${p.quality.counts.error} · 警告 ${p.quality.counts.warn} · 提示 ${p.quality.counts.info}` },
        ];
        // k-cls 对应 CSS 里的左侧色条，让有问题的指标一眼可见
        $('#kpiGrid').innerHTML = kpis.map(k => `
            <div class="kpi ${k.cls ? 'k-' + k.cls : ''}">
                <div class="kpi-label">${k.l}</div>
                <div class="kpi-value ${k.cls || ''}">${k.v}</div>
                <div class="kpi-hint">${esc(k.h)}</div>
            </div>`).join('');

        const score = p.quality.score, C = 2 * Math.PI * 40, arc = $('#scoreArc');
        arc.setAttribute('stroke-dasharray', C.toFixed(1));
        arc.setAttribute('stroke-dashoffset', (C * (1 - score / 100)).toFixed(1));
        arc.setAttribute('stroke', score >= 80 ? '#2e9e6b' : score >= 60 ? '#d98824' : '#d64541');
        $('#scoreVal').textContent = score;
        $('#scoreGrade').textContent = p.quality.grade;

        const c = p.quality.counts, maxV = Math.max(1, c.error, c.warn, c.info);
        $('#scoreBars').innerHTML = [
            { lbl: '严重', n: c.error, color: '#d64541' },
            { lbl: '警告', n: c.warn, color: '#d98824' },
            { lbl: '提示', n: c.info, color: '#3b6fd4' },
        ].map(r => `<div class="score-bar-row"><span class="lbl">${r.lbl}</span>
            <span class="score-bar-track"><span class="score-bar-fill"
              style="width:${(r.n / maxV * 100).toFixed(0)}%;background:${r.color}"></span></span>
            <span class="cnt">${r.n}</span></div>`).join('');
        $('#qualitySub').textContent = `耗时 ${p._meta.elapsed_ms}ms`;

        const issues = p.quality.issues;
        if (!issues.length) {
            $('#issueTable').innerHTML = '<div class="empty">没查出明显问题，这份数据挺干净。</div>';
        } else {
            const lvlCN = { error: '严重', warn: '警告', info: '提示' };
            $('#issueTable').innerHTML = `<div class="tbl-wrap" style="max-height:340px"><table class="tbl">
                <thead><tr><th style="width:60px">级别</th><th style="width:120px">列</th>
                <th>问题</th><th style="width:34%">建议与修复代码</th></tr></thead>
                <tbody>${issues.map(i => `<tr>
                    <td><span class="badge badge-${i.level}">${lvlCN[i.level]}</span></td>
                    <td class="mono">${esc(i.column || '—')}</td>
                    <td>${esc(i.message)}</td>
                    <td>${esc(i.suggestion)}
                      <details class="fix"><summary>查看 pandas 代码</summary>
                      <pre>${esc(i.pandas_code)}</pre></details></td></tr>`).join('')}</tbody></table></div>`;
        }

        $('#colCount').textContent = `共 ${p.columns.length} 列`;
        $('#colTable').innerHTML = `<thead><tr><th>列名</th><th>类型</th><th>角色</th>
            <th class="num">缺失率</th><th class="num">唯一值</th><th>摘要</th></tr></thead>
            <tbody>${p.columns.map(col => {
            const st = col.stats || {};
            let summary = '';
            if (st.mean !== undefined) {
                summary = `均值 ${fmtNum(st.mean)} · 中位 ${fmtNum(st.p50)} · 范围 [${fmtNum(st.min)}, ${fmtNum(st.max)}]`
                    + (st.outliers ? ` · <span style="color:var(--warn)">异常 ${st.outliers}</span>` : '');
            } else if (st.top_values && st.top_values.length) {
                summary = st.top_values.slice(0, 3).map(t => `${esc(t.value)} <span class="hint">${(t.pct * 100).toFixed(1)}%</span>`).join(' · ');
            } else if (st.span_days !== undefined) {
                summary = `${st.min} → ${st.max} · 跨度 ${st.span_days} 天`;
            }
            return `<tr><td class="mono" style="font-weight:600">${esc(col.name)}</td>
                <td class="mono hint">${esc(col.dtype)}</td>
                <td><span class="badge badge-muted">${esc(col.role)}</span></td>
                <td class="num" style="color:${col.missing_pct > .3 ? 'var(--danger)' : 'inherit'}">${fmtPct(col.missing_pct)}</td>
                <td class="num">${fmtNum(col.unique)}</td>
                <td style="max-width:380px">${summary}</td></tr>`;
        }).join('')}</tbody>`;
    }

    async function renderCorrCharts() {
        if (!S.active) return;
        const post = (spec, target) => api('/api/chart', {
            method: 'POST', body: { sid: S.sid, dataset: S.active, spec, ...themeArg() },
        }).then(r => drawOption(target, reviveOption(r.data.option)))
            .catch(e => { $(target).innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
        post({ chart: 'corr' }, '#chartCorr');
        post({ chart: 'missing_bar' }, '#chartMiss');
    }

    // ============================================================ 可视化

    /* 「你想看什么」引导。

       大部分人不是先想「我要画柱状图」，而是先想「哪个城市卖得最好」。
       所以先问目的，再替他选图 —— 这比丢一堆图表类型让他自己猜友好得多。 */
    // 分析目的对应的图标，纯装饰，帮助快速识别
    const INTENT_ICONS = {
        compare: '📊', trend: '📈', part: '🥧', dist: '📶',
        relation: '🔗', rank: '🏆', quality: '🩺', mix: '⚖',
    };

    function renderIntents() {
        const host = $('#intentGrid');
        if (!host) return;
        const intents = S.catalog.intents || [];
        host.innerHTML = intents.map(it =>
            `<button class="intent ${S.intent === it.key ? 'on' : ''}" data-in="${esc(it.key)}">
               <div class="intent-q">${INTENT_ICONS[it.key] || '▦'} ${esc(it.q)}</div>
               <div class="intent-a">${esc(it.a)}</div>
             </button>`).join('');
        $$('#intentGrid .intent').forEach(btn => btn.onclick = () => applyIntent(btn.dataset.in));
    }

    function applyIntent(key) {
        const it = (S.catalog.intents || []).find(x => x.key === key);
        if (!it) return;
        if (!S.active) { toast('请先载入数据集', 'error'); return; }
        S.intent = key;
        renderIntents();
        if (!S.charts.length) {
            const spec = normalizeSpec(Object.assign(defaultSpec(), { chart: it.charts[0] }));
            addChart(spec);
        } else {
            const c = S.charts[0];
            c.spec.chart = it.charts[0];
            normalizeSpec(c.spec);
            renderChartList();
        }
        toast(`图表类型已切换为 ${it.charts[0]}`, 'ok');
    }

    /* 换图表类型之后，X / Y 轴那些参数未必还适用（比如直方图的 X 必须是数值列），
       统一在这里纠正，省得每个入口各写一遍。 */
    function normalizeSpec(sp) {
        const meta = (S.catalog.charts || []).find(x => x.chart === sp.chart) || {};
        const nums = numCols(), dims = dimCols();
        const pool = (sp.chart === 'histogram' || sp.chart === 'scatter') ? nums : S.columns;
        if (meta.need_x) {
            if (!sp.x || !pool.some(c => c.name === sp.x)) sp.x = (pool[0] || {}).name;
        }
        if (meta.need_y) {
            const ys = (sp.ys || []).filter(Boolean);
            if (!ys.length) sp.ys = nums.slice(0, 1).map(c => c.name);
            if (!meta.multi_y) sp.ys = sp.ys.slice(0, 1);
        }
        if (!meta.agg) sp.agg = 'sum';
        if (!meta.group) sp.group = '';
        if (!meta.limit) sp.limit = 0;
        if (!meta.multi_y && sp.secondary) sp.secondary = [];
        return sp;
    }

    function defaultSpec() {
        const dims = dimCols(), nums = numCols();
        return {
            chart: 'bar',
            x: (dims[0] || S.columns[0] || {}).name,
            ys: nums.slice(0, 1).map(c => c.name),
            agg: 'sum', limit: 30, sort: 'desc', group: '',
            stack: false, area: false, smooth: true, label: false,
            secondary: [], bins: 30, horizontal: false,
        };
    }

    function ensureDefaultChart() {
        if (!S.active) { S.charts = []; renderChartList(); return; }
        if (S.charts.length) { S.charts.forEach(c => refreshChart(c)); return; }
        addChart();
    }

    function addChart(spec) {
        if (!S.active) { toast('请先载入数据集', 'error'); return; }
        const c = { id: 'c' + (++S.chartSeq), spec: spec || defaultSpec() };
        S.charts.push(c);
        renderChartList();
        refreshChart(c);
    }

    function opt(name, value, label, selected) {
        const sel = selected ? ' selected' : '';
        return `<option value="${esc(value)}"${sel}>${esc(label)}</option>`;
    }

    function chartCardHtml(c) {
        const meta = S.catalog.charts.find(x => x.chart === c.spec.chart) || S.catalog.charts[0];
        const sp = c.spec;
        const nums = numCols();

        let controls = '';
        // 图表类型
        controls += `<div class="field" style="max-width:210px"><label>图表类型</label>
            <select data-key="chart">${S.catalog.charts.map(x => opt('chart', x.chart, x.label, x.chart === sp.chart)).join('')}</select></div>`;

        // X 轴：直方图和散点图只能吃数值列，别把类别列摆出来误导
        if (meta.need_x) {
            const xPool = (sp.chart === 'histogram' || sp.chart === 'scatter') ? nums : S.columns;
            controls += `<div class="field"><label>${sp.chart === 'histogram' ? '数值列' : '维度 / X 轴'}</label>
                <select data-key="x">${xPool.map(x => opt('x', x.name, `${x.name}（${x.role}）`, x.name === sp.x)).join('')}</select></div>`;
        }
        // Y 轴（多选 = 多指标）
        if (meta.need_y) {
            const multi = !!meta.multi_y;
            controls += `<div class="field"><label>${multi ? '指标 / Y 轴（按住 Ctrl 可多选，一图多线）' : '指标 / Y 轴'}</label>
                <select data-key="ys" ${multi ? 'multiple size="4"' : ''}>
                ${nums.map(x => opt('ys', x.name, x.name, (sp.ys || []).indexOf(x.name) >= 0)).join('')}
                </select></div>`;
        }
        // 聚合
        if (meta.agg) {
            controls += `<div class="field" style="max-width:120px"><label>聚合</label>
                <select data-key="agg">${S.catalog.aggs.map(a => opt('agg', a.value, a.label, a.value === sp.agg)).join('')}</select></div>`;
        }
        // 分组
        if (meta.group) {
            controls += `<div class="field" style="max-width:150px"><label>颜色分组（可选）</label>
                <select data-key="group"><option value="">— 不分组 —</option>
                ${dimCols().map(x => opt('group', x.name, x.name, x.name === sp.group)).join('')}</select></div>`;
        }
        // TopN
        if (meta.limit) {
            controls += `<div class="field" style="max-width:96px"><label>Top N</label>
                <input type="number" data-key="limit" value="${sp.limit}" min="1" max="200"></div>`;
        }
        // 排序
        if (meta.agg) {
            controls += `<div class="field" style="max-width:150px"><label>排序</label>
                <select data-key="sort">${S.catalog.sorts.map(s => opt('sort', s.value, s.label, s.value === sp.sort)).join('')}</select></div>`;
        }
        // 双轴 secondary
        if (meta.secondary) {
            controls += `<div class="field"><label>右轴指标（画成折线，Ctrl 多选）</label>
                <select data-key="secondary" multiple size="3">
                ${nums.map(x => opt('secondary', x.name, x.name, (sp.secondary || []).indexOf(x.name) >= 0)).join('')}</select></div>`;
        }
        if (meta.bins) {
            controls += `<div class="field" style="max-width:96px"><label>分箱数</label>
                <input type="number" data-key="bins" value="${sp.bins}" min="3" max="100"></div>`;
        }

        let toggles = '';
        [['stack', '堆叠'], ['area', '面积填充'], ['smooth', '平滑曲线'],
        ['show_label', '显示数值'], ['horizontal', '横向显示']].forEach(([k, label]) => {
            if (!meta[k]) return;
            toggles += `<label class="chk"><input type="checkbox" data-toggle="${k}" ${sp[k] ? 'checked' : ''}> ${label}</label>`;
        });

        return `
        <div class="card chart-card" data-chart-id="${c.id}">
            <div class="card-head">
                <h3 class="sub">${esc(meta.desc)}</h3>
                <div class="row">
                    <button class="btn btn-sm" data-act="png">导出 PNG</button>
                    <button class="btn btn-sm" data-act="csv">导出数据</button>
                    <button class="btn btn-sm btn-danger" data-act="del">删除</button>
                </div>
            </div>
            <div class="card-body">
                <div class="form-row">${controls}</div>
                ${toggles ? `<div class="chk-row">${toggles}</div>` : ''}
                <div class="chart-canvas chart-box" style="height:${sp.chart === 'pie' || sp.chart === 'ring' ? '380px' : '420px'}"></div>
                <div class="hint" data-role="status" style="margin-top:6px"></div>
            </div>
        </div>`;
    }

    function renderChartList() {
        const host = $('#chartHost');
        host.innerHTML = '';
        if (!S.charts.length) {
            host.innerHTML = '<div class="empty">点右上角「再加一张图」开始分析</div>';
            return;
        }
        S.charts.forEach(c => {
            const wrap = document.createElement('div');
            wrap.innerHTML = chartCardHtml(c);
            const card = wrap.firstElementChild;
            host.appendChild(card);

            $$('[data-key]', card).forEach(sel => sel.addEventListener('change', () => {
                const k = sel.dataset.key;
                if (k === 'ys' || k === 'secondary') {
                    c.spec[k] = Array.from(sel.selectedOptions).map(o => o.value);
                } else if (k === 'limit' || k === 'bins') {
                    c.spec[k] = parseInt(sel.value, 10) || (k === 'bins' ? 30 : 30);
                } else {
                    c.spec[k] = sel.value;
                }
                if (k === 'chart') {
                    normalizeSpec(c.spec);
                    S.intent = '';        // 手动换过图，就不再是「按意图选」的状态
                    renderIntents();
                    renderChartList();
                }
                refreshChart(c);
            }));

            $$('[data-toggle]', card).forEach(cb => cb.addEventListener('change', () => {
                c.spec[cb.dataset.toggle] = cb.checked;
                refreshChart(c);
            }));

            const canvas = $('.chart-canvas', card);
            $('[data-act="del"]', card).onclick = () => {
                S.charts = S.charts.filter(x => x.id !== c.id);
                renderChartList();
            };
            $('[data-act="png"]', card).onclick = () => exportChartPng(c);
            $('[data-act="csv"]', card).onclick = () => exportChartCsv(c);
        });
    }

    async function refreshChart(c) {
        const card = $(`.card[data-chart-id="${c.id}"]`);
        if (!card) return;
        const canvas = $('.chart-canvas', card), status = $('[data-role="status"]', card);
        const spec = Object.assign({}, c.spec);
        ['corr', 'missing_map', 'missing_bar'].includes(spec.chart) && (spec.x = spec.group = null);
        try {
            const r = await api('/api/chart', {
                method: 'POST',
                body: { sid: S.sid, dataset: S.active, spec, ...themeArg() },
            });
            drawOption(canvas, reviveOption(r.data.option));
            status.textContent = `渲染 ${r.data.elapsed_ms}ms · 数据集 ${S.active}`;
        } catch (e) {
            canvas.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
            status.textContent = '';
        }
    }

    function exportChartPng(c) {
        const card = $(`.card[data-chart-id="${c.id}"]`);
        const canvas = $('.chart-canvas', card);
        const inst = _chartInst.get(canvas);
        if (!inst) { toast('图表尚未渲染', 'error'); return; }
        const a = document.createElement('a');
        a.href = inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#fff' });
        a.download = `chart_${S.active}_${Date.now()}.png`;
        a.click();
        toast('已导出图片', 'ok');
    }

    async function exportChartCsv(c) {
        try {
            const r = await api('/api/chart', {
                method: 'POST',
                body: { sid: S.sid, dataset: S.active, spec: c.spec, ...themeArg() },
            });
            const o = r.data.option;
            const cats = (o.xAxis && o.xAxis.data) || (Array.isArray(o.xAxis) ? o.xAxis[0].data : null);
            if (!cats || !o.series) { toast('该图没有可导出的二维数据', 'error'); return; }
            const series = Array.isArray(o.series) ? o.series : [o.series];
            const usable = series.filter(s => Array.isArray(s.data));
            if (!usable.length) { toast('该图没有可导出的二维数据', 'error'); return; }
            const head = ['类别'].concat(usable.map(s => s.name || 'series'));
            const lines = [head.join(',')];
            for (let i = 0; i < cats.length; i++) {
                lines.push([cats[i]].concat(usable.map(s => {
                    const v = s.data[i];
                    if (v == null) return '';
                    if (Array.isArray(v)) return v.join(' ');
                    if (typeof v === 'object') return JSON.stringify(v);
                    return v;
                })).join(','));
            }
            const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `chart_${S.active}_${Date.now()}.csv`;
            a.click();
            toast('已导出图表数据', 'ok');
        } catch (e) { toast(e.message, 'error'); }
    }

    // ============================================================ 清洗

    /* 场景模板：把高频需求直接映射成一组算子，不用用户自己拼。
       命名对齐主流 BI / ETL 工具的叫法（替换值、拆分列、分组汇总…）。 */
    const SCENARIOS = [
        {
            key: 'unit', icon: '📏', name: '提取数值',
            desc: '从带单位的文本中提取数值，如面积、规格、时长',
            fields: [{ key: 'column', type: 'column', label: '列' }],
            build: v => [{
                op: 'extract_number', params: {
                    column: v.column, mode: 'first', decimals: true, keep_original: false }
            }],
        },
        {
            key: 'dedup', icon: '🧹', name: '删除重复项',
            desc: '按指定列或整行去除重复记录',
            fields: [{ key: 'columns', type: 'columns', label: '依据列', optional: true }],
            build: v => [{ op: 'drop_duplicates', params: { subset: v.columns, keep: 'first' } }],
        },
        {
            key: 'fill', icon: '🩹', name: '填充空值',
            desc: '按中位数、众数等策略填充空值',
            fields: [
                { key: 'column', type: 'column', label: '列' },
                {
                    key: 'strategy', type: 'select', label: '策略', default: 'median',
                    options: [['median', '中位数'], ['mode', '众数'], ['mean', '平均值'],
                              ['zero', '0'], ['ffill', '向前填充']],
                },
            ],
            build: v => [{ op: 'fillna', params: { column: v.column, strategy: v.strategy } }],
        },
        {
            key: 'replace', icon: '✏️', name: '替换值',
            desc: '统一同义表述，或清除固定文本',
            fields: [
                { key: 'column', type: 'column', label: '列' },
                { key: 'pairs', type: 'pairs', label: '替换规则' },
            ],
            build: v => [{
                op: 'replace', params: { column: v.column, pairs: v.pairs, mode: 'substring' }
            }],
        },
        {
            key: 'split', icon: '✂️', name: '拆分列',
            desc: '按分隔符将单列拆分为多列',
            fields: [
                { key: 'column', type: 'column', label: '列' },
                { key: 'sep', type: 'text', label: '分隔符', default: '/' },
            ],
            build: v => [{ op: 'split_column', params: { column: v.column, sep: v.sep, n: 0 } }],
        },
        {
            key: 'filter', icon: '🔍', name: '筛选行',
            desc: '按条件保留符合要求的记录',
            fields: [
                { key: 'column', type: 'column', label: '列' },
                {
                    key: 'operator', type: 'select', label: '条件', default: '=',
                    options: [['=', '等于'], ['!=', '不等于'], ['>', '大于'], ['>=', '大于等于'],
                              ['<', '小于'], ['<=', '小于等于'], ['contains', '包含'],
                              ['is_null', '为空'], ['not_null', '不为空']],
                },
                { key: 'value', type: 'text', label: '值', default: '' },
            ],
            build: v => (v.operator === 'is_null' || v.operator === 'not_null')
                ? [{ op: 'filter', params: { column: v.column, operator: v.operator } }]
                : [{ op: 'filter', params: { column: v.column, operator: v.operator, value: v.value } }],
        },
        {
            key: 'dates', icon: '📅', name: '转换日期',
            desc: '将日期文本转换为 datetime',
            fields: [],
            build: () => [{ op: 'auto_convert_dates', params: { threshold: '0.9', fmt: '' } }],
        },
        {
            key: 'tidy', icon: '🧽', name: '基础清理',
            desc: '修整空格、统一大小写、删除常量列',
            fields: [],
            build: () => [
                { op: 'strip', params: { columns: null } },
                { op: 'lowercase', params: { upper: false, columns: null } },
                { op: 'drop_constant', params: { drop_all_null: true } },
            ],
        },
    ];

    function renderScenarios() {
        const host = $('#scenarioGrid');
        if (!host) return;
        host.innerHTML = SCENARIOS.map(s =>
            `<button class="scenario" data-sc="${esc(s.key)}">
               <div class="scenario-name"><span>${s.icon}</span>${esc(s.name)}</div>
               <div class="scenario-desc">${esc(s.desc)}</div>
             </button>`).join('');
        $$('#scenarioGrid .scenario').forEach(btn => {
            btn.onclick = () => openScenario(SCENARIOS.find(x => x.key === btn.dataset.sc));
        });
    }

    function openScenario(sc) {
        if (!sc) return;
        if (!S.active) { toast('请先载入数据集', 'error'); return; }
        // 不需要参数的直接加，一步到位
        if (!sc.fields.length) { pushScenarioOps(sc.build({}), sc.name); return; }

        const body = sc.fields.map(f => {
            if (f.type === 'column') {
                return `<div class="field"><label>${esc(f.label)}</label>
                    <select data-sf="${esc(f.key)}">${S.columns.map(c =>
                        `<option value="${esc(c.name)}">${esc(c.name)}（${esc(c.role)}）</option>`).join('')}</select></div>`;
            }
            if (f.type === 'columns') {
                return `<div class="field"><label>${esc(f.label)}
                    ${f.optional ? '<span class="hint">留空 = 整行完全相同才算重复</span>' : ''}</label>
                    <select data-sf="${esc(f.key)}" multiple size="4">${S.columns.map(c =>
                        `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('')}</select></div>`;
            }
            if (f.type === 'select') {
                return `<div class="field"><label>${esc(f.label)}</label>
                    <select data-sf="${esc(f.key)}">${f.options.map(([v, t]) =>
                        `<option value="${esc(v)}" ${v === f.default ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>`;
            }
            if (f.type === 'pairs') {
                return `<div class="field"><label>${esc(f.label)}</label>
                    <textarea rows="4" data-sf="${esc(f.key)}"
                      placeholder="㎡ =&gt;        &lt;-- 右边留空就是删掉&#10;门店 =&gt; 线下门店&#10;抖音直播 =&gt; 抖音"></textarea></div>`;
            }
            return `<div class="field"><label>${esc(f.label)}</label>
                <input type="text" data-sf="${esc(f.key)}" value="${esc(f.default || '')}"
                  placeholder="${esc(f.placeholder || '')}"></div>`;
        }).join('');

        modal(sc.name,
            `<div class="op-intro" style="margin-bottom:14px"><span>${sc.icon}</span>
               <div>${esc(sc.desc)}</div></div>${body}`,
            [
                {
                    label: '添加这些步骤', primary: true, onClick: (m) => {
                        const v = {};
                        $$('[data-sf]', m).forEach(el => {
                            if (el.multiple) v[el.dataset.sf] = Array.from(el.selectedOptions).map(o => o.value);
                            else v[el.dataset.sf] = el.value;
                        });
                        if (v.pairs !== undefined) {
                            v.pairs = parsePairs(v.pairs);
                            if (!v.pairs.length) { toast('至少要写一条替换规则', 'error'); return; }
                        }
                        if (v.columns !== undefined && !v.columns.length) v.columns = null;
                        closeModal();
                        pushScenarioOps(sc.build(v), sc.name);
                    }
                },
                { label: '取消', onClick: closeModal },
            ]);
    }

    function pushScenarioOps(ops, name) {
        if (!ops || !ops.length) { toast('未生成清洗步骤', 'error'); return; }
        S.cleanOps.push(...ops);
        renderSteps();
        toast(`已添加 ${ops.length} 个步骤`, 'ok');
    }

    /* 把「原值 => 新值」文本解析成规则数组。有 => 就按 => 拆，
       否则才退回用逗号 —— 不能无条件按逗号拆，'1,234 => 1234' 会被切碎。 */
    function parsePairs(text) {
        return String(text || '').split('\n').map(l => l.trim())
            .filter(l => l && !l.startsWith('#'))
            .map(l => {
                const parts = (/=>|->/.test(l) ? l.split(/=>|->/) : l.split(','))
                    .map(s => s.trim());
                return { from: parts[0], to: parts.length > 1 ? parts[1] : '' };
            })
            .filter(p => p.from);
    }

    function renderOpSelect() {
        // 按用途分组：用户是带着「我要解决什么问题」来的，不是带着算子名来的
        const groups = S.catalog.op_groups || [{ group: '', ops: S.catalog.ops || [] }];
        $('#opSelect').innerHTML = groups.map(g =>
            `<optgroup label="${esc(g.group)}">${g.ops.map(o =>
                `<option value="${esc(o.op)}">${esc(o.label)}</option>`).join('')}</optgroup>`
        ).join('');
        updateOpDesc();
    }

    const currentOpMeta = () => {
        const key = $('#opSelect').value;
        return (S.catalog.ops || []).find(o => o.op === key) || {};
    };

    const COL_HINTS = {
        single: '单选',
        multi: '支持多选（Ctrl / Shift）',
        multi_optional: '不选则作用于整表',
        none: '无需选列',
    };

    function updateOpDesc() {
        const m = currentOpMeta();
        const mode = m.col_mode || 'none';
        const sel = $('#opColumns');
        sel.disabled = mode === 'none';
        sel.multiple = mode === 'multi' || mode === 'multi_optional';
        sel.size = sel.multiple ? 5 : 1;
        $('#colHint').textContent = COL_HINTS[mode] || '';

        // 使用场景：说明该操作的适用条件
        $('#opWhen').innerHTML = m.when
            ? `<div class="when-box"><b>使用场景：</b>${esc(m.when)}</div>` : '';
        // 示例：展示处理前后的取值变化
        $('#opExample').innerHTML = (m.example && m.example.before != null)
            ? `<div class="op-example">
                 <span class="ex-label">示例</span>
                 <span class="ex-flow">
                   <span class="ex-before">${esc(m.example.before)}</span>
                   <span class="ex-arrow">→</span>
                   <span class="ex-after">${esc(m.example.after)}</span>
                 </span>
               </div>` : '';
        updateOpExtra();
    }

    function updateOpExtra() {
        const m = currentOpMeta();
        const host = $('#opExtra');
        host.innerHTML = '';
        (m.extra || []).forEach(f => {
            const wrap = document.createElement('div');
            wrap.className = 'field';
            wrap.style.maxWidth = '230px';
            const label = f.label || f.key;
            let inner = '';
            if (f.type === 'select') {
                // options 里的值可能带 labels 映射（值保持机器可读，给人看的是中文说明）
                inner = `<label>${esc(label)}</label><select data-extra="${f.key}">${
                    f.options.map(o => {
                        const text = (f.labels && f.labels[o]) ? f.labels[o] : o;
                        return `<option value="${o}" ${o === f.default ? 'selected' : ''}>${esc(text)}</option>`;
                    }).join('')}</select>`;
            } else if (f.type === 'bool') {
                inner = `<label>${esc(label)}</label><select data-extra="${f.key}">
                    <option value="false" ${!f.default ? 'selected' : ''}>否</option>
                    <option value="true" ${f.default ? 'selected' : ''}>是</option></select>`;
            } else if (f.type === 'number') {
                inner = `<label>${esc(label)}</label>
                    <input type="number" data-extra="${f.key}" value="${esc(f.default)}" placeholder="${esc(f.placeholder || '')}">`;
            } else if (f.type === 'pairs') {
                inner = `<label>${esc(label)}（每行一条：原值 =&gt; 新值；新值留空表示删掉）</label>
                    <textarea rows="3" data-extra="${f.key}" placeholder="㎡ =&gt;        ← 只留数字&#10;门店 =&gt; 线下门店&#10;抖音直播 =&gt; 抖音"></textarea>`;
            } else if (f.type === 'columns') {
                inner = `<label>${esc(label)}</label><input type="text" data-extra="${f.key}" placeholder="city, channel">`;
            } else if (f.type === 'metrics') {
                inner = `<label>${esc(label)}</label>
                    <textarea rows="3" data-extra="${f.key}" placeholder="amount,sum&#10;order_id,nunique"></textarea>`;
            } else {
                inner = `<label>${esc(label)}</label>
                    <input type="text" data-extra="${f.key}" placeholder="${esc(f.placeholder || '')}" value="${esc(f.default || '')}">`;
            }
            wrap.innerHTML = inner;
            host.appendChild(wrap);
        });
    }

    function renderOpColumns() {
        const sel = $('#opColumns');
        const keep = new Set(Array.from(sel.options).filter(o => o.selected).map(o => o.value));
        sel.innerHTML = S.columns.map(c => `<option value="${esc(c.name)}" ${keep.has(c.name) ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
    }

    function collectOp() {
        const meta = currentOpMeta();
        const op = meta.op, mode = meta.col_mode || 'none';
        const params = {};
        const cols = Array.from($('#opColumns').selectedOptions).map(o => o.value);

        if (mode === 'single') {
            if (!cols.length) throw new Error(`「${meta.label}」需要指定作用列，请在上面的列表里选一列`);
            params.column = cols[0];
        } else if (mode === 'multi') {
            if (!cols.length) throw new Error(`「${meta.label}」需要至少选择一列`);
            params.columns = cols;
        } else if (mode === 'multi_optional') {
            params.columns = cols.length ? cols : null;
        }

        $$('[data-extra]').forEach(el => {
            const k = el.dataset.extra;
            let v = el.value;
            if (el.type === 'checkbox') v = el.checked;
            else if (el.tagName === 'SELECT' && el.multiple) v = Array.from(el.selectedOptions).map(o => o.value);
            else if (el.type === 'number') v = parseFloat(el.value);
            params[k] = v;
        });

        // subset / columns 的命名差别，按算子的实际签名收口
        if (op === 'drop_duplicates') {
            if (params.columns && params.columns.length) params.subset = params.columns;
            delete params.columns;
        }
        if (op === 'dropna') {
            if (!params.columns || !params.columns.length) delete params.columns;
        }
        if (op === 'replace') {
            params.pairs = String(params.pairs || '').split('\n').map(l => l.trim())
                .filter(l => l && !l.startsWith('#'))
                .map(l => {
                    // 有 => 或 -> 就按它拆，否则退回用逗号拆；
                    // 不能无条件用逗号拆，否则 '1,234 => 1234' 会被切碎
                    const parts = (/=>|->/.test(l) ? l.split(/=>|->/) : l.split(','))
                        .map(s => s.trim());
                    return { from: parts[0], to: parts.length > 1 ? parts[1] : '' };
                })
                .filter(p => p.from);
            if (!params.pairs.length) throw new Error('至少要写一条替换规则');
        }
        if (op === 'group_agg') {
            params.by = String(params.by || '').split(',').map(s => s.trim()).filter(Boolean);
            params.metrics = String(params.metrics || '').split('\n').map(l => l.trim()).filter(Boolean)
                .map(l => { const p = l.split(',').map(s => s.trim()); return { column: p[0], agg: p[1] || 'sum' }; });
            if (!params.by.length || !params.metrics.length) throw new Error('分组聚合需要填「分组列」和「聚合指标」');
        }
        if (op === 'split_column' && !String(params.sep || '').length) throw new Error('分隔符不能为空');
        if (op === 'fillna' && params.strategy === 'group_mode' && !String(params.by || '').trim())
            throw new Error('分组填充需要指定分组列');
        if (op === 'fillna' && params.strategy === 'constant' && String(params.value || '') === '')
            throw new Error('常量填充需要填写 value');
        return { op, params, label: meta.label };
    }

    function renderSteps() {
        const host = $('#stepList');
        $('#pipeCount').textContent = S.cleanOps.length ? `${S.cleanOps.length} 步` : '';
        if (!S.cleanOps.length) {
            host.innerHTML = '<div class="empty">还没有步骤。在上面选好操作和列，点「添加为步骤」。</div>';
        } else {
            host.innerHTML = S.cleanOps.map((s, i) => {
                const p = s.params || {};
                let desc = '';
                if (p.column) desc += `列: ${p.column}　`;
                if (p.columns && p.columns.length) desc += `列: ${p.columns.join(', ')}　`;
                Object.keys(p).forEach(k => {
                    if (['column', 'columns'].includes(k) || p[k] === '' || p[k] == null) return;
                    if (Array.isArray(p[k])) { if (p[k].length) desc += `${k}: ${p[k].map(x => JSON.stringify(x)).join(' ')}　`; }
                    else desc += `${k}: ${p[k]}　`;
                });
                return `<div class="step-card"><div class="step-idx">${i + 1}</div>
                    <div class="step-main"><div class="step-title">${esc(s.label)}
                    <span class="hint mono">${esc(s.op)}</span></div>
                    <div class="step-desc">${esc(desc) || '—'}</div></div>
                    <button class="btn btn-sm btn-danger" data-i="${i}">移除</button></div>`;
            }).join('');
            $$('[data-i]', host).forEach(b => b.onclick = () => {
                S.cleanOps.splice(parseInt(b.dataset.i, 10), 1); renderSteps();
            });
        }
    }

    function updateCodeBox(code) {
        if (!code) return;
        $('#codeBox').textContent = code;
        const blob = new Blob([code], { type: 'text/plain;charset=utf-8' });
        $('#btnDownloadCode').href = URL.createObjectURL(blob);
    }

    function resetCleanOps() {
        S.cleanOps = [];
        renderSteps();
        renderOpColumns();
        $('#codeBox').textContent = '执行清洗后会在这里生成完整脚本。';
        $('#cleanDiff').innerHTML = '';
    }

    // ============================================================ SQL
    async function loadSchema() {
        if (!S.sid) return;
        try {
            const r = await api('/api/sql/tables?sid=' + S.sid);
            const host = $('#schemaList');
            if (!r.data.length) {
                host.innerHTML = '<div class="empty">还没有表。载入数据后自动注册到 SQL 引擎。</div>';
                return;
            }
            host.innerHTML = r.data.map(t => `
                <div class="schema-item" data-t="${esc(t.table)}">
                    <div class="tname">${esc(t.table)}</div>
                    <div class="tinfo">${fmtNum(t.rows)} 行</div>
                    <div class="schema-cols">${t.columns.map(c => esc(c)).join(', ')}</div>
                </div>`).join('');
            $$('.schema-item', host).forEach(el => el.onclick = () => {
                $('#sqlText').value = `SELECT *\nFROM "${el.dataset.t}"\nLIMIT 100`;
            });
        } catch (e) { }
    }

    async function runSql() {
        const sql = $('#sqlText').value.trim();
        if (!sql) { toast('SQL 不能为空', 'error'); return; }
        $('#sqlStatus').innerHTML = '<span class="loading"></span> 执行中';
        try {
            const r = await api('/api/sql', { method: 'POST', body: { sid: S.sid, sql, limit: parseInt($('#sqlLimit').value, 10) } });
            const d = r.data;
            $('#sqlResultMeta').textContent = `${fmtNum(d.rowcount)} 行 · ${d.elapsed_ms}ms${d.truncated ? ' · 已截断到上限' : ''}`;
            $('#sqlStatus').textContent = '';
            $('#sqlTable').innerHTML = `<thead><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
                <tbody>${d.rows.map(row => `<tr>${row.map(v =>
                `<td class="${typeof v === 'number' ? 'num' : ''}">${v === null || v === undefined ? '<span class="hint">NULL</span>' : esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>`;
            if (!d.rows.length) $('#sqlTable').innerHTML += '<tbody><tr><td class="empty">查询结果为空</td></tr></tbody>';
        } catch (e) {
            $('#sqlStatus').textContent = e.message;
            $('#sqlTable').innerHTML = `<tbody><tr><td class="empty" style="color:var(--danger)">${esc(e.message)}</td></tr></tbody>`;
        }
    }

    // ============================================================ 数据对比
    async function runCompare() {
        const left = $('#cmpLeft').value, right = $('#cmpRight').value, key = $('#cmpKey').value;
        if (!left || !right) { toast('请选择两份数据集', 'error'); return; }
        if (left === right) { toast('请选择两份不同的数据集', 'error'); return; }
        $('#cmpHint').innerHTML = '<span class="loading"></span> 对比中…';
        try {
            const r = await api('/api/compare/run', { method: 'POST', body: { sid: S.sid, left, right, key } });
            const d = r.data;
            $('#cmpHint').textContent = `耗时 ${d._meta.elapsed_ms}ms`;
            renderCompare(d, left, right);
        } catch (e) {
            $('#cmpHint').textContent = e.message;
            toast(e.message, 'error');
        }
    }

    function renderCompare(d, leftName, rightName) {
        const st = d.structure, rows = d.rows, drift = d.drift;
        let html = '';

        // 结构
        const colDiff = (st.only_in_left.length || st.only_in_right.length || st.dtype_changed.length);
        html += `<div class="card"><div class="card-head"><h3>结构对比</h3>
            <span class="hint">行数 ${fmtNum(st.rows_left)} → ${fmtNum(st.rows_right)}
            （${st.rows_delta >= 0 ? '+' : ''}${fmtNum(st.rows_delta)}）</span></div>
            <div class="card-body">
            <div class="kpi-grid">
                <div class="kpi"><div class="kpi-label">基准行数</div><div class="kpi-value">${fmtNum(st.rows_left)}</div>
                    <div class="kpi-hint">${leftName}</div></div>
                <div class="kpi"><div class="kpi-label">对比行数</div>
                    <div class="kpi-value ${st.rows_delta < 0 ? 'danger' : ''}">${fmtNum(st.rows_right)}</div>
                    <div class="kpi-hint">${rightName}</div></div>
                <div class="kpi"><div class="kpi-label">共有列</div><div class="kpi-value">${st.common_columns.length}</div>
                    <div class="kpi-hint">左 ${st.cols_left} 列 / 右 ${st.cols_right} 列</div></div>
                <div class="kpi"><div class="kpi-label">结构是否一致</div>
                    <div class="kpi-value ${colDiff ? 'warn' : ''}">${colDiff ? '有差异' : '一致'}</div>
                    <div class="kpi-hint">新增 ${st.only_in_right.length} · 缺失 ${st.only_in_left.length} · 类型变化 ${st.dtype_changed.length}</div></div>
            </div>`;
        if (colDiff) {
            html += `<div class="cmp-cols">
                <div><div class="cmp-ct">只在基准中（${st.only_in_left.length}）</div>
                    ${st.only_in_left.length ? st.only_in_left.map(c => `<span class="tag tag-out">${esc(c)}</span>`).join('') : '<span class="hint">无</span>'}</div>
                <div><div class="cmp-ct">新增的列（${st.only_in_right.length}）</div>
                    ${st.only_in_right.length ? st.only_in_right.map(c => `<span class="tag tag-new">${esc(c)}</span>`).join('') : '<span class="hint">无</span>'}</div>
                <div><div class="cmp-ct">类型变化（${st.dtype_changed.length}）</div>
                    ${st.dtype_changed.length ? st.dtype_changed.map(c =>
                `<span class="tag">${esc(c.column)}: ${esc(c.left)} → ${esc(c.right)}</span>`).join('') : '<span class="hint">无</span>'}</div>
            </div>`;
        }
        html += `</div></div>`;

        // 行级
        if (rows) {
            html += `<div class="card"><div class="card-head"><h3>行级差异 <span class="sub">主键 ${esc(rows.key)}</span></h3></div>
                <div class="card-body">
                <div class="kpi-grid">
                    <div class="kpi"><div class="kpi-label">新增的行</div>
                        <div class="kpi-value">${fmtNum(rows.only_right_rows)}</div>
                        <div class="kpi-hint">只存在于对比数据</div></div>
                    <div class="kpi"><div class="kpi-label">消失的行</div>
                        <div class="kpi-value danger">${fmtNum(rows.only_left_rows)}</div>
                        <div class="kpi-hint">只存在于基准数据</div></div>
                    <div class="kpi"><div class="kpi-label">键值被修改</div>
                        <div class="kpi-value warn">${fmtNum(rows.changed_rows)}</div>
                        <div class="kpi-hint">匹配上 ${fmtNum(rows.matched_rows)} 行</div></div>
                    <div class="kpi"><div class="kpi-label">未变化</div>
                        <div class="kpi-value">${fmtNum(rows.unchanged_rows)}</div>
                        <div class="kpi-hint">完全一致</div></div>
                </div>`;
            if (rows.key_has_duplicates)
                html += `<div class="warn-note">注意：主键在这两份数据里存在重复
                    （左 ${rows.dupe_rows_left} / 右 ${rows.dupe_rows_right} 行），
                    已按首条去重后比对，结果可能不完整。</div>`;
            if (rows.changed_columns && rows.changed_columns.length) {
                html += `<div class="tbl-wrap" style="max-height:220px"><table class="tbl">
                    <thead><tr><th>字段</th><th class="num">被修改的行数</th></tr></thead>
                    <tbody>${rows.changed_columns.map(c => `<tr><td class="mono">${esc(c.column)}</td>
                    <td class="num">${fmtNum(c.changed_rows)}</td></tr>`).join('')}</tbody></table></div>`;
            }
            if (rows.samples && rows.samples.length) {
                html += `<div class="hint" style="margin:12px 0 4px">差异明细（最多 ${rows.samples.length} 条）：</div>
                    <div class="tbl-wrap" style="max-height:280px"><table class="tbl">
                    <thead><tr><th>${esc(rows.key)}</th><th>字段</th><th>基准值</th><th>对比值</th></tr></thead>
                    <tbody>${rows.samples.map(s => `<tr><td class="mono">${esc(s._key)}</td>
                        <td class="mono">${esc(s.column)}</td>
                        <td class="del">${esc(s.left)}</td><td class="ins">${esc(s.right)}</td></tr>`).join('')}</tbody></table></div>`;
            }
            html += `</div></div>`;
        }

        // 漂移
        if (drift.numeric && drift.numeric.length) {
            html += `<div class="card"><div class="card-head">
                <h3>数值列分布漂移 <span class="sub">均值变化超过 20% 会标红</span></h3></div>
                <div class="card-body">
                <div id="cmpDriftChart" class="chart-box sm"></div>
                <div class="tbl-wrap" style="max-height:340px;margin-top:10px"><table class="tbl">
                <thead><tr><th>字段</th><th class="num">均值(基准)</th><th class="num">均值(对比)</th>
                <th class="num">变化</th><th class="num">缺失率变化</th></tr></thead>
                <tbody>${drift.numeric.map(r => `<tr>
                    <td class="mono">${esc(r.column)}</td>
                    <td class="num">${fmtNum(r.left_mean)}</td>
                    <td class="num">${fmtNum(r.right_mean)}</td>
                    <td class="num ${r.alert ? 'del' : ''}">${fmtSigned(r.mean_delta_pct)}</td>
                    <td class="num">${fmtPct(r.left_null_pct)} → ${fmtPct(r.right_null_pct)}</td>
                </tr>`).join('')}</tbody></table></div></div></div>`;
        }
        if (drift.categorical && drift.categorical.length) {
            html += `<div class="card"><div class="card-head"><h3>类别列构成变化</h3></div>
                <div class="card-body">${drift.categorical.map(c => `
                <div class="cmp-cat">
                    <div class="cmp-cat-h">${esc(c.column)}
                        <span class="hint">取值数 ${c.left_unique} → ${c.right_unique}</span></div>
                    ${c.new_values.length ? `<div>新增取值：${c.new_values.map(v => `<span class="tag tag-new">${esc(v)}</span>`).join('')}</div>` : ''}
                    ${c.missing_values.length ? `<div>消失取值：${c.missing_values.map(v => `<span class="tag tag-out">${esc(v)}</span>`).join('')}</div>` : ''}
                </div>`).join('')}</div></div>`;
        }

        $('#cmpResult').innerHTML = html;

        S.cmpCtx = {
            left: leftName, right: rightName,
            hasDrift: !!(drift.numeric && drift.numeric.length),
        };
        renderCmpChart();
    }

    /* 漂移图单独拎出来，是为了切主题时能原样重画一次 */
    function renderCmpChart() {
        const ctx = S.cmpCtx;
        const el = $('#cmpDriftChart');
        if (!ctx || !ctx.hasDrift || !el) return;
        api('/api/compare/chart', {
            method: 'POST',
            body: { sid: S.sid, left: ctx.left, right: ctx.right, kind: 'drift', ...themeArg() },
        }).then(r => drawOption(el, reviveOption(r.data)))
            .catch(e => { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
    }

    // ============================================================ 明细
    /* 创建数据源（落表）：取数 → 写入。
       和「连接数据库」是两件事 —— 那个是连上去读，这个是把数据写出去。 */
    function createSourceModal() {
        if (!S.datasets.length) { toast('请先载入数据集', 'error'); return; }
        const srcOpts = (S.sources || []).filter(s => !s.error)
            .map(s => `<option value="${esc(s.cid)}">${esc(s.label)}（${esc(s.dbtype)}）</option>`).join('');
        const dsOpts = S.datasets.map(d =>
            `<option value="${esc(d.name)}">${esc(d.name)}（${fmtNum(d.rows)} 行）</option>`).join('');
        const noSrc = srcOpts || '<option value="">（暂无可用数据源）</option>';

        const html = `
            <div class="op-group-title">数据来源</div>
            <div class="row" style="gap:16px">
                <label class="chk"><input type="radio" name="srcKind" value="dataset" checked> 数据集</label>
                <label class="chk"><input type="radio" name="srcKind" value="sql"> SQL 查询</label>
                <label class="chk"><input type="radio" name="srcKind" value="table"> 数据源的表</label>
            </div>
            <div id="srcBody" style="margin-top:10px"></div>

            <div class="op-group-title">写入目标</div>
            <div class="row" style="gap:16px">
                <label class="chk"><input type="radio" name="tgtKind" value="sqlite_file" checked> 新建本地 SQLite 文件</label>
                <label class="chk"><input type="radio" name="tgtKind" value="source"> 已连接的数据源</label>
            </div>
            <div id="tgtBody" style="margin-top:10px"></div>

            <div class="form-row" style="margin-top:14px">
                <div class="field"><label>目标表名</label>
                    <input type="text" id="csTable" placeholder="如 orders_2025"></div>
                <div class="field" style="max-width:210px"><label>写入模式</label>
                    <select id="csMode">
                        <option value="replace">覆盖（重建表）</option>
                        <option value="append">追加到末尾</option>
                        <option value="fail">表已存在则报错</option>
                    </select></div>
            </div>
            <label class="chk" style="margin-top:6px">
                <input type="checkbox" id="csRegister" checked> 写完后加入左侧数据源列表</label>
            <div id="csPreview" style="margin-top:12px"></div>`;

        const m = modal('创建数据源', html, [
            { label: '预览数据', onClick: () => previewCreate(m) },
            { label: '执行写入', primary: true, onClick: () => doCreate(m) },
            { label: '取消', onClick: closeModal },
        ]);

        const checked = (name) => {
            const el = m.querySelector(`[name=${name}]:checked`);
            return el ? el.value : '';
        };
        const get = (k) => {
            const el = m.querySelector(`[data-cs="${k}"]`);
            return el ? el.value : '';
        };

        function renderSrcBody() {
            const kind = checked('srcKind');
            const host = m.querySelector('#srcBody');
            if (kind === 'dataset') {
                host.innerHTML = `<div class="field" style="max-width:340px"><label>数据集</label>
                    <select data-cs="srcName">${dsOpts}</select></div>`;
            } else if (kind === 'sql') {
                host.innerHTML = `
                    <div class="field" style="max-width:340px"><label>执行环境</label>
                        <select data-cs="srcOn">
                            <option value="">当前会话（数据集引擎）</option>${noSrc}
                        </select></div>
                    <div class="field"><label>SQL（仅支持查询）</label>
                        <textarea rows="5" data-cs="srcSql"
                          placeholder="SELECT city, SUM(amount) AS gmv FROM ecommerce_orders GROUP BY city"></textarea></div>`;
            } else {
                host.innerHTML = `<div class="form-row">
                    <div class="field" style="max-width:260px"><label>数据源</label>
                        <select data-cs="srcCid">${noSrc}</select></div>
                    <div class="field" style="max-width:260px"><label>表</label>
                        <input type="text" data-cs="srcTable" placeholder="表名"></div></div>`;
            }
        }

        function renderTgtBody() {
            const kind = checked('tgtKind');
            const host = m.querySelector('#tgtBody');
            if (kind === 'sqlite_file') {
                host.innerHTML = `<div class="field"><label>文件路径</label>
                    <div class="row"><input type="text" data-cs="tgtPath" placeholder="如 D:\\data\\result.sqlite" style="flex:1">
                    <button class="btn btn-sm" id="csPick">浏览</button></div></div>`;
                host.querySelector('#csPick').onclick = () => {
                    browseFiles('', (p) => { host.querySelector('[data-cs="tgtPath"]').value = p; });
                };
            } else {
                host.innerHTML = `<div class="field" style="max-width:340px"><label>目标数据源</label>
                    <select data-cs="tgtCid">${noSrc}</select></div>`;
            }
        }

        function collect() {
            const srcKind = checked('srcKind');
            const tgtKind = checked('tgtKind');
            const source = { kind: srcKind };
            if (srcKind === 'dataset') source.name = get('srcName');
            else if (srcKind === 'sql') { source.sql = get('srcSql'); source.on = get('srcOn') || null; }
            else { source.cid = get('srcCid'); source.table = get('srcTable'); }

            const target = { kind: tgtKind };
            if (tgtKind === 'sqlite_file') target.path = get('tgtPath');
            else target.cid = get('tgtCid');

            return {
                sid: S.sid, source, target,
                table: m.querySelector('#csTable').value.trim(),
                mode: m.querySelector('#csMode').value,
                register: m.querySelector('#csRegister').checked,
            };
        }

        async function previewCreate() {
            try {
                const r = await api('/api/datasource/preview', { method: 'POST', body: collect() });
                const d = r.data;
                m.querySelector('#csPreview').innerHTML = `
                    <div class="hint">共 ${fmtNum(d.total)} 行 · ${d.columns.length} 列，预览前 ${d.rows.length} 行</div>
                    <div class="tbl-wrap" style="max-height:200px;margin-top:6px"><table class="tbl">
                    <thead><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
                    <tbody>${d.rows.map(row => `<tr>${row.map(v =>
                        `<td>${v == null ? '<span class="hint">NULL</span>' : esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
            } catch (e) { toast(e.message, 'error'); }
        }

        async function doCreate() {
            const payload = collect();
            if (!payload.table) { toast('请填写目标表名', 'error'); return; }
            if (payload.target.kind === 'sqlite_file' && !payload.target.path) {
                toast('请选择 SQLite 文件路径', 'error'); return;
            }
            if (payload.target.kind === 'source' && !payload.target.cid) {
                toast('请选择目标数据源', 'error'); return;
            }
            try {
                const r = await api('/api/datasource/create', { method: 'POST', body: payload });
                toast(r.message || '写入完成', 'ok');
                closeModal();
                await loadSources();
            } catch (e) { toast(e.message, 'error'); }
        }

        m.querySelectorAll('[name=srcKind]').forEach(el => el.onchange = renderSrcBody);
        m.querySelectorAll('[name=tgtKind]').forEach(el => el.onchange = renderTgtBody);
        renderSrcBody();
        renderTgtBody();
    }

    // ============================================================ 明细筛选与排序
    function renderFilterControls() {
        const opts = S.columns.map(c =>
            `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
        $('#fCol').innerHTML = opts;
        $('#sCol').innerHTML = '<option value="">不排序</option>' + opts;
        $('#fOp').innerHTML = FILTER_OPS.map(([v, t]) =>
            `<option value="${esc(v)}">${esc(t)}</option>`).join('');
        $('#sCol').value = S.browseSort.column || '';
        $('#sDir').value = S.browseSort.ascending ? 'asc' : 'desc';
        updateFilterHint();
    }

    function updateFilterHint() {
        const op = $('#fOp').value;
        const hints = {
            between: '两个值，逗号分隔，如 10,100',
            in: '多个值，逗号分隔',
            is_null: '无需填值',
            not_null: '无需填值',
        };
        $('#fValHint').textContent = hints[op] || '';
        $('#fVal').disabled = NO_VALUE_OPS.includes(op);
    }

    function renderFilterChips() {
        const host = $('#filterChips');
        if (!S.filters.length) {
            host.innerHTML = '<span class="hint">未设置筛选条件</span>';
            return;
        }
        host.innerHTML = S.filters.map((f, i) => {
            const val = NO_VALUE_OPS.includes(f.operator)
                ? ''
                : ` <b>${esc(f.operator === 'between' ? `${f.value}~${f.value2}` : f.value)}</b>`;
            return `<span class="tag">${esc(f.column)} <span class="hint">${esc(opText(f.operator))}</span>${val}
                <button class="tag-x" data-fi="${i}" title="移除">×</button></span>`;
        }).join('');
        $$('#filterChips .tag-x').forEach(b => b.onclick = () => {
            S.filters.splice(parseInt(b.dataset.fi, 10), 1);
            renderFilterChips();
            loadPreview();
        });
    }

    function addFilter() {
        const col = $('#fCol').value;
        const operator = $('#fOp').value;
        const raw = $('#fVal').value.trim();
        if (!col) { toast('请选择筛选列', 'error'); return; }
        if (!NO_VALUE_OPS.includes(operator) && !raw) { toast('请填写筛选值', 'error'); return; }

        const f = { column: col, operator, value: raw };
        if (operator === 'between') {
            const parts = raw.split(',').map(s => s.trim());
            if (parts.length < 2 || !parts[0] || !parts[1]) {
                toast('「介于」需要两个值，用逗号分隔', 'error');
                return;
            }
            f.value = parts[0];
            f.value2 = parts[1];
        }
        S.filters.push(f);
        $('#fVal').value = '';
        renderFilterChips();
        loadPreview();
    }

    function clearFilters() {
        S.filters = [];
        S.browseSort = { column: '', ascending: true };
        $('#sCol').value = '';
        $('#sDir').value = 'asc';
        renderFilterChips();
        loadPreview();
    }

    async function loadPreview() {
        if (!S.active) return;
        try {
            // 走 browse 接口：筛选和排序都作用在全量数据上，而不是当前这一页
            const r = await api('/api/dataset/' + encodeURIComponent(S.active) + '/browse', {
                method: 'POST',
                body: {
                    sid: S.sid,
                    n: parseInt($('#previewN').value, 10) || 100,
                    filters: S.filters,
                    sort: S.browseSort,
                },
            });
            const d = r.data;
            const scope = S.filters.length
                ? `筛选后 ${fmtNum(d.total)} / ${fmtNum(d.before)} 行`
                : `共 ${fmtNum(d.total)} 行`;
            $('#dataMeta').textContent = `${scope}，显示 ${fmtNum(d.shown)} 行`;
            // 带行号：数据核对时好定位（主流 BI 表格的常规做法）
            $('#dataTable').innerHTML = `<thead><tr><th class="rowno">#</th>${d.columns.map((c, i) =>
                `<th>${esc(c)}<div class="hint" style="font-weight:400">${esc(d.dtypes[i])}</div></th>`).join('')}</tr></thead>
                <tbody>${d.rows.map((row, ri) => `<tr><td class="rowno">${ri + 1}</td>${row.map(v =>
                `<td class="${typeof v === 'number' ? 'num' : ''}">${v === null || v === undefined ? '<span class="hint">NULL</span>' : esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>`;
            if (!d.rows.length) {
                $('#dataTable').innerHTML += '<tbody><tr><td class="empty">没有符合条件的数据</td></tr></tbody>';
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ============================================================ 文件上传 / 本地浏览
    async function uploadFiles(files) {
        for (const f of files) {
            const fd = new FormData();
            fd.append('file', f);
            try {
                const r = await api('/api/upload', { method: 'POST', body: fd });
                toast(`已载入 ${r.data.map(x => x.name).join('、')}`, 'ok');
                await refreshDatasets(r.data[0] && r.data[0].name);
            } catch (e) { toast(`${f.name}：${e.message}`, 'error'); }
        }
    }

    /* 本机文件浏览器：支持所有盘符 + 快捷目录 + 路径直接输入 */
    async function browseFiles(path, onPickFile) {
        let r, d;
        try {
            r = await api('/api/file/pick?path=' + encodeURIComponent(path || ''));
            d = r.data;
        } catch (e) { toast(e.message, 'error'); return; }

        const chips = (d.drives || []).map(dr =>
            `<button class="chip" data-go="${esc(dr.path)}">${esc(dr.name)}</button>`).join('')
            + (d.quick || []).map(q =>
                `<button class="chip chip-quick" data-go="${esc(q.path)}">${esc(q.name)}</button>`).join('');

        let bodyHtml = '';
        if (d.level === 'root') {
            bodyHtml = '<div class="empty">选一个磁盘，或者在下面直接粘贴路径跳转</div>';
        } else {
            const dirs = d.entries.filter(e => e.is_dir);
            const files = d.entries.filter(e => !e.is_dir);
            bodyHtml = `<div class="tbl-wrap" style="max-height:380px"><table class="tbl"><tbody>
                ${dirs.map(e => `<tr data-dir="${esc(e.path)}"><td>📁 ${esc(e.name)}</td><td></td></tr>`).join('')}
                ${files.map(e => `<tr data-file="${esc(e.path)}" ${e.readable ? 'style="cursor:pointer"' : 'style="opacity:.45"'}>
                    <td>${e.readable ? '📄' : '·'} ${esc(e.name)}</td>
                    <td class="hint" style="text-align:right">${e.readable ? fmtBytes(e.size) : '不支持'}</td></tr>`).join('')}
                </tbody></table></div>`;
        }

        const html = `
            <div class="quick-row">${chips}</div>
            <div class="row" style="margin:8px 0">
                <input type="text" id="fsPath" value="${esc(d.cwd)}" placeholder="直接粘贴路径，如 D:\\data\\sales.csv">
                <button class="btn btn-sm" data-go-input>跳转</button>
                <span class="spacer"></span>
                ${d.parent === '' && d.level === 'dir' ? '<button class="btn btn-sm" data-home>回到磁盘列表</button>' : ''}
                ${d.parent ? '<button class="btn btn-sm" data-up>上一级</button>' : ''}
            </div>
            ${d.level === 'dir' ? `<div class="hint" style="margin-bottom:6px">当前：<span class="mono">${esc(d.cwd)}</span></div>` : ''}
            ${bodyHtml}`;

        const buttons = onPickFile
            ? [{ label: '关闭', onClick: closeModal }]
            : [];
        const m = modal('打开本机数据文件', html, buttons);

        $$('[data-go]', m).forEach(el => el.onclick = () => browseFiles(el.dataset.go, onPickFile));
        $('[data-go-input]', m).onclick = () => browseFiles($('#fsPath', m).value, onPickFile);
        $('#fsPath', m).addEventListener('keydown', e => {
            if (e.key === 'Enter') browseFiles($('#fsPath', m).value, onPickFile);
        });
        const up = $('[data-up]', m);
        if (up) up.onclick = () => browseFiles(d.parent, onPickFile);
        const home = $('[data-home]', m);
        if (home) home.onclick = () => browseFiles('', onPickFile);
        $$('[data-dir]', m).forEach(el => el.onclick = () => browseFiles(el.dataset.dir, onPickFile));

        $$('[data-file]', m).forEach(el => el.onclick = async () => {
            const p = el.dataset.file;
            if (onPickFile) { onPickFile(p); closeModal(); return; }
            try {
                const rr = await api('/api/load_path', { method: 'POST', body: { sid: S.sid, path: p } });
                toast('已载入 ' + rr.data.map(x => x.name).join('、'), 'ok');
                closeModal();
                await refreshDatasets(rr.data[0] && rr.data[0].name);
            } catch (e) { toast(e.message, 'error'); }
        });
    }

    function doExport() {
        if (!S.active) { toast('请先选择数据集', 'error'); return; }
        const fmt = $('#exportFmt').value;
        window.location.href = `/api/export/${encodeURIComponent(S.active)}?sid=${S.sid}&fmt=${fmt}`;
        toast('已开始导出 ' + fmt.toUpperCase());
    }

    async function doReport() {
        if (!S.active) { toast('请先选择数据集', 'error'); return; }
        try {
            const r = await api('/api/report/' + encodeURIComponent(S.active), { method: 'POST', body: { sid: S.sid } });
            const md = r.data.markdown;
            modal('数据分析报告', `<pre class="mono" style="white-space:pre-wrap;margin:0">${esc(md)}</pre>`, [
                { label: '复制 Markdown', primary: true, onClick: () => navigator.clipboard.writeText(md).then(() => toast('已复制', 'ok')) },
                {
                    label: '下载 .md', onClick: () => {
                        const a = document.createElement('a');
                        a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
                        a.download = `report_${S.active}.md`; a.click();
                    },
                },
                { label: '关闭', onClick: closeModal },
            ]);
        } catch (e) { toast(e.message, 'error'); }
    }

    // ============================================================ 事件绑定
    function bindEvents() {
        // 主题：先把当前状态同步到按钮上（首次进来时不重绘，图还没画）
        applyTheme(curTheme(), false);
        const tb = $('#btnTheme');
        if (tb) tb.onclick = toggleTheme;

        $$('.tab').forEach(t => t.onclick = () => {
            $$('.tab').forEach(x => x.classList.remove('active'));
            $$('.view').forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            $('#view-' + t.dataset.view).classList.add('active');
            requestAnimationFrame(() => $$('.chart-box').forEach(el => {
                const inst = _chartInst.get(el); if (inst) inst.resize();
            }));
        });

        const dz = $('#dropzone');
        dz.onclick = () => $('#fileInput').click();
        $('#fileInput').onchange = (e) => uploadFiles(Array.from(e.target.files));
        ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
        ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('over'); }));
        dz.addEventListener('drop', e => uploadFiles(Array.from(e.dataTransfer.files)));

        $('#btnBrowse').onclick = () => browseFiles('');
        $('#btnDb').onclick = dbModal;
        $('#btnDb2').onclick = dbModal;
        $('#btnReset').onclick = async () => {
            await initSession();
            S.active = null; S.datasets = []; S.charts = []; S.columns = [];
            renderDatasets(); renderChartList(); loadSchema(); loadSources();
            $('#edaEmpty').style.display = ''; $('#edaBody').style.display = 'none';
            $('#ctxTitle').textContent = '未选择数据集'; $('#ctxSub').textContent = '';
            toast('会话已重置', 'ok');
        };

        $('#btnExport').onclick = doExport;
        $('#btnReport').onclick = doReport;

        $('#btnAddChart').onclick = () => addChart();

        $('#opSelect').onchange = updateOpDesc;
        $('#btnAddOp').onclick = () => {
            try {
                const step = collectOp();
                S.cleanOps.push(step);
                renderSteps();
                toast('已添加步骤 ' + step.label, 'ok');
            } catch (e) { toast(e.message, 'error'); }
        };
        $('#btnClearOps').onclick = () => { S.cleanOps = []; renderSteps(); };
        $('#btnPreviewClean').onclick = async () => {
            if (!S.cleanOps.length) { toast('未添加清洗步骤', 'error'); return; }
            try {
                const r = await api('/api/clean/preview', { method: 'POST', body: { sid: S.sid, dataset: S.active, ops: S.cleanOps } });
                const d = r.data;
                // 把后端给的 warning 单独拎出来提示，别埋在 JSON 里没人看
                const warns = d.steps
                    .filter(s => s.effect && s.effect.warning)
                    .map(s => `第 ${s.index} 步「${s.label}」：${s.effect.warning}`);
                $('#cleanDiff').innerHTML = `
                    <div class="kpi" style="margin-top:12px">
                        <div class="kpi-label">执行效果预览</div>
                        <div class="kpi-value">${fmtNum(d.before.rows)} → ${fmtNum(d.after.rows)} <span class="hint">行</span></div>
                        <div class="kpi-hint">${d.before.cols} → ${d.after.cols} 列 ·
                            行变化 ${d.after.rows - d.before.rows >= 0 ? '+' : ''}${d.after.rows - d.before.rows}</div>
                    </div>
                    ${warns.length ? `<div class="warn-note">${warns.map(esc).join('<br>')}</div>` : ''}
                    <div class="tbl-wrap" style="max-height:220px;margin-top:8px"><table class="tbl">
                        <thead><tr><th>#</th><th>步骤</th><th class="num">执行后行数</th><th>效果</th></tr></thead>
                        <tbody>${d.steps.map(s => `<tr><td>${s.index}</td><td>${esc(s.label)}</td>
                            <td class="num">${fmtNum(s.rows)}</td>
                            <td class="mono" style="font-size:11px">${esc(JSON.stringify(s.effect))}</td></tr>`).join('')}</tbody></table></div>`;
                updateCodeBox(d.code);
            } catch (e) { toast(e.message, 'error'); }
        };
        $('#btnApplyClean').onclick = async () => {
            if (!S.cleanOps.length) { toast('未添加清洗步骤', 'error'); return; }
            try {
                const r = await api('/api/clean/apply', { method: 'POST', body: { sid: S.sid, dataset: S.active, ops: S.cleanOps } });
                const d = r.data;
                updateCodeBox(d.code);
                toast(`已生成数据集 ${d.name}（${d.before.rows} → ${d.after.rows} 行）`, 'ok');
                await refreshDatasets(d.name);
                S.cleanOps = []; renderSteps();
            } catch (e) { toast(e.message, 'error'); }
        };
        $('#btnCopyCode').onclick = () => navigator.clipboard.writeText($('#codeBox').textContent)
            .then(() => toast('已复制', 'ok'));

        $('#btnRunSql').onclick = runSql;
        $('#sqlTpl').onchange = () => {
            const i = $('#sqlTpl').value;
            if (i === '') return;
            $('#sqlText').value = SQL_TPL[parseInt(i, 10)].sql;
            $('#sqlTpl').value = '';
        };
        $('#btnSaveSql').onclick = async () => {
            const sql = $('#sqlText').value.trim();
            if (!sql) return;
            const name = (prompt('存为数据集，名字是？', 'sql_result') || '').trim();
            if (!name) return;
            try {
                const r = await api('/api/sql/save', { method: 'POST', body: { sid: S.sid, sql, save_as: name } });
                toast(`已保存为 ${r.data.name}`, 'ok');
                await refreshDatasets(r.data.name);
            } catch (e) { toast(e.message, 'error'); }
        };
        $('#sqlText').addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); runSql(); }
        });

        $('#btnCompare').onclick = runCompare;

        $('#previewN').onchange = loadPreview;
        $('#btnRefreshPreview').onclick = loadPreview;
        // 明细筛选 / 排序
        $('#btnAddFilter').onclick = addFilter;
        $('#btnClearFilter').onclick = clearFilters;
        $('#fOp').onchange = updateFilterHint;
        $('#sCol').onchange = () => { S.browseSort.column = $('#sCol').value; loadPreview(); };
        $('#sDir').onchange = () => {
            S.browseSort.ascending = $('#sDir').value === 'asc';
            loadPreview();
        };

        window.addEventListener('resize', () => $$('.chart-box').forEach(el => {
            const inst = _chartInst.get(el); if (inst) inst.resize();
        }));
    }

    // ============================================================ 启动
    (async function boot() {
        bindEvents();
        try {
            await initSession();
            await loadCatalog();
            await loadSources();
        } catch (e) {
            toast('初始化失败：' + e.message, 'error');
        }
        renderDatasets();
        renderChartList();
        loadSchema();
    })();
})();
