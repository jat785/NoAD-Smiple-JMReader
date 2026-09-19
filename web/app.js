/* JMReader 前端 —— 无框架、无构建、无外部依赖。
 *
 * 路由（hash）：
 *   #/                          首页：随机 N 部（顺序列表）
 *   #/search?q=&by=&order=&time=&page=
 *   #/discover                  分类 / 标签 / 排行
 *   #/album/<id>                详情
 *   #/read/<chapterId>?album=&local=&page=
 *   #/favorites  #/history  #/library  #/library/<id>  #/tasks  #/settings
 */
(() => {
  'use strict';

  const app = document.getElementById('app');
  const toastEl = document.getElementById('toast');

  // ------------------------------------------------------------ 基础工具

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  let toastTimer = 0;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
  }

  function fmtTime(unixSec) {
    if (!unixSec) return '';
    const d = new Date(unixSec * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
    return sameDay ? hm : `${d.getMonth() + 1}-${p(d.getDate())} ${hm}`;
  }

  // 「记住密码」的说明。后端会把真实强度报上来 ——
  // Windows 上是 DPAPI（真的加密），其它系统只是本地密钥文件的 AES（混淆级），
  // 这两种情况必须说清楚，不能让用户以为到哪都一样安全。
  function rememberNote(acc) {
    const b = acc.secret_backend || {};
    const base = '禁漫的登录状态只有几小时有效期，过期后需要重新登录。'
      + '勾选后本程序会用下面的机制把密码存起来，失效时自动重登。';
    if (b.secure) {
      return `<div class="kv" style="color:var(--muted);margin-top:6px">
        ${base}<br>
        当前机制：<b>${esc(b.label || '')}</b>。${esc(b.note || '')}
      </div>`;
    }
    return `<div class="notice warn" style="margin:8px 0 0">
      ${base}<br><br>
      当前机制：<b>${esc(b.label || '未知')}</b>。<b>它不保证安全。</b>${esc(b.note || '')}
      <br><br>想要真正安全，请在 Windows 上运行本程序，或不要勾选这一项。
    </div>`;
  }

  // 会话失效的判断不能靠字符串猜。后端现在把 401 明确成 HTTP 401，
  // 所以优先看状态码；老版本/兜底再退回到关键词（简体「登录」与繁体「登入」都要认）。
  function isAuthError(err) {
    if (err && err.status === 401) return true;
    const m = String((err && err.message) || '');
    return m.includes('登录') || m.includes('登入') || m.includes('未登录');
  }

  // 视图切换时要回收的监听器。少了它，反复进出阅读器会叠加 keydown 处理器。
  let cleanups = [];
  function onCleanup(fn) { cleanups.push(fn); }

  async function api(path, options) {
    // no-store：这些全是实时数据，一旦被浏览器或中间代理复用旧响应，
    // 就会变成「扫描到 4 部、列表却只有 1 部」这种极难排查的幽灵问题
    const res = await fetch(path, { cache: 'no-store', ...options });
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch { data = null; } }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      // 带上状态码：调用方据 401 判断"登录失效"，比猜错误文案可靠得多
      err.status = res.status;
      throw err;
    }
    return data;
  }
  const apiGet = (p) => api(p);
  const apiPost = (p, body) => api(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const apiDel = (p) => api(p, { method: 'DELETE' });

  function setMain(html) { app.innerHTML = html; }

  function renderError(err) {
    setMain(`<div class="error">出错了：${esc(err.message || err)}</div>`);
  }

  // ------------------------------------------------------------ 复用片段

  function comicRow(item, index, hrefBuilder) {
    // 照搬禁漫网页端的卡片：封面里叠作者（左上）、分类徽章（右上）、收藏标（右下），
    // 封面下面是标题 / 橙色作者 / 灰色标签行。
    const author = item.author || '';
    const cat = item.category || '';
    const fav = item.is_favorite ? '<span class="ov ov-br" title="已收藏">🔖</span>' : '';
    // 禁漫的列表接口不返回单本标签，拿不到时用发布日期/体积把第三行填上
    const line3 = [...(item.tags || []), item.adddate || ''].filter(Boolean).join(' ');
    const href = hrefBuilder ? hrefBuilder(item) : `#/album/${esc(item.album_id)}`;

    return `
      <a class="comic-card" href="${esc(href)}">
        ${index === undefined ? '' : `<span class="idx">${index}</span>`}
        <span class="thumb">
          <img loading="lazy" src="${esc(item.cover || '')}" alt="" referrerpolicy="no-referrer">
          ${author ? `<span class="ov ov-tl">${esc(author)}</span>` : ''}
          ${cat ? `<span class="ov ov-tr"><i class="badge">${esc(cat)}</i></span>` : ''}
          ${fav}
        </span>
        <span class="info">
          <span class="title">${esc(item.title || '(无标题)')}</span>
          ${author ? `<span class="author">${esc(author)}</span>` : ''}
          ${line3 ? `<span class="tags">${esc(line3)}</span>` : ''}
        </span>
      </a>`;
  }

  function comicList(items, opts = {}) {
    if (!items || !items.length) return '<div class="empty">没有结果</div>';
    const numbered = opts.numbered === true;
    return `<div class="comic-grid">${
      items.map((it, i) => comicRow(it, numbered ? i + 1 : undefined, opts.href)).join('')
    }</div>`;
  }

  function pager(page, hasNext, makeHref) {
    return `<div class="pager">
      ${page > 1 ? `<a class="btn" href="${esc(makeHref(page - 1))}">上一页</a>` : ''}
      <span>第 ${page} 页</span>
      ${hasNext ? `<a class="btn" href="${esc(makeHref(page + 1))}">下一页</a>` : ''}
    </div>`;
  }

  // 点击标签 → 打开标签筛选页并把它选上（而不是直接搜这个标签）
  document.addEventListener('click', (ev) => {
    const tag = ev.target.closest('[data-tag]');
    if (tag) {
      ev.preventDefault();
      location.hash = `#/tags?sel=${encodeURIComponent(tag.dataset.tag)}`;
    }
  });

  // ------------------------------------------------------------ 排版与主题

  const LAYOUT_KEY = 'jmreader.layout';   // auto | grid | list
  const THEME_KEY = 'jmreader.theme';     // auto | dark | light
  const WIDE = '(min-width: 720px)';

  function applyLayout() {
    const pref = localStorage.getItem(LAYOUT_KEY) || 'auto';
    const wide = window.matchMedia(WIDE).matches;
    // 宽屏默认网格、窄屏默认列表；用户也可以手动钉死其中一种
    document.body.dataset.layout = pref === 'auto' ? (wide ? 'grid' : 'list') : pref;
    document.body.dataset.view = pref;
    const btn = document.getElementById('btn-layout');
    if (btn) {
      btn.textContent = { auto: '▦ 自适应', grid: '▦ 网格', list: '☰ 列表' }[pref];
      btn.title = '排版：自适应 / 网格 / 列表';
    }
  }

  function applyTheme() {
    const pref = localStorage.getItem(THEME_KEY) || 'auto';
    if (pref === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = pref;
    const btn = document.getElementById('btn-theme');
    if (btn) {
      btn.textContent = { auto: '◐ 跟随系统', dark: '◑ 深色', light: '◒ 浅色' }[pref];
      btn.title = '主题：跟随系统 / 深色 / 浅色';
    }
  }

  function cyclePref(key, order, apply) {
    const cur = localStorage.getItem(key) || 'auto';
    localStorage.setItem(key, order[(order.indexOf(cur) + 1) % order.length]);
    apply();
  }

  document.getElementById('btn-layout').onclick = () => cyclePref(LAYOUT_KEY, ['auto', 'grid', 'list'], applyLayout);
  document.getElementById('btn-theme').onclick = () => cyclePref(THEME_KEY, ['auto', 'dark', 'light'], applyTheme);
  window.addEventListener('resize', applyLayout);
  applyLayout();
  applyTheme();

  // ------------------------------------------------------------ 返回

  // 站内访问过的 hash 序列。用它来判断"上一条是哪儿"，
  // 这样浏览器自带的后退键和页面里的「返回」按钮走的是同一套逻辑。
  const navStack = [];

  function currentHash() { return location.hash || '#/'; }

  /** 记录一次路由跳转，返回是否是"后退"。 */
  function syncNavStack(hash) {
    const n = navStack.length;
    if (n >= 2 && navStack[n - 2] === hash) {
      navStack.pop();                    // 后退（浏览器后退键或页面里的返回）
      return true;
    }
    if (n >= 1 && navStack[n - 1] === hash) {
      return false;                      // 原地重渲染，不动栈
    }
    navStack.push(hash);                 // 前进到新页面
    if (navStack.length > 60) navStack.shift();
    return false;
  }

  /** 没有上一页时（比如直接打开链接/刷新）该退到哪。 */
  function fallbackFor(hash) {
    const parts = hash.split('?')[0].replace(/^#/, '').split('/').filter(Boolean);
    if (parts[0] === 'library' && parts[1]) return '#/library';
    return '#/';
  }

  function goBack(fallback) {
    if (navStack.length >= 2) history.back();
    else location.hash = fallback || '#/';
  }

  function updateBackButton(hash) {
    const btn = document.getElementById('btn-back');
    if (btn) btn.hidden = (hash === '#/' || hash === '');
  }

  document.getElementById('btn-back').onclick = () => goBack(fallbackFor(currentHash()));

  // ------------------------------------------------------------ 视图

  async function viewHome() {
    setMain('<div class="loading">加载中…</div>');
    const data = await apiGet('/api/home');
    setMain(`
      <h1 class="page-title">随便看看 <small>默认页随机 ${data.count} 部</small></h1>
      <div class="toolbar">
        <button class="btn primary" id="reroll">换一批</button>
        <a class="btn" href="#/discover">去发现</a>
      </div>
      ${comicList(data.items, { numbered: true })}
    `);
    document.getElementById('reroll').onclick = viewHome;
  }

  function parseQuery(hash) {
    const qIndex = hash.indexOf('?');
    return new URLSearchParams(qIndex >= 0 ? hash.slice(qIndex + 1) : '');
  }

  // 纯数字就当车牌号（社区里说的"jm 号"）
  const JM_ID_RE = /^\d{1,9}$/;

  /** 把详情接口返回的本子整形卡片能用的形状。 */
  function albumToCard(a) {
    return {
      album_id: a.album_id,
      title: a.title,
      author: (a.authors || []).join('、'),
      tags: a.tags || [],
      cover: `/api/cover/${a.album_id}`,
      adddate: a.pub_date || '',
    };
  }

  async function viewSearch(params) {
    const q = params.get('q') || '';
    const by = params.get('by') || 'site';
    const order = params.get('order') || 'mr';
    const time = params.get('time') || 'a';
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);

    if (!q) { setMain('<div class="empty">请输入搜索词</div>'); return; }

    // ---- 车牌号精确定位 ----
    // 只在「关键词」模式下、且整串都是数字时才当车号。
    // 用户显式选了作者/标签等类型时，就老老实实按那个类型搜。
    let jmMiss = null;       // 没命中时的说明，会显示在关键词结果上方
    let jmError = null;      // 接口本身出问题（区别于"查无此号"）
    if (by === 'site' && JM_ID_RE.test(q)) {
      setMain('<div class="loading">正在按 JM 号定位…</div>');
      try {
        const hit = await apiGet(`/api/lookup/${encodeURIComponent(q)}`);
        if (hit.found) {
          const a = hit.album;
          setMain(`
            <h1 class="page-title">JM 号定位 <small>精确匹配</small></h1>
            ${comicList([albumToCard(a)])}
            <div class="toolbar" style="margin-top:12px">
              <span class="kv">JM${esc(a.album_id)} · ${a.chapters ? a.chapters.length : 0} 话 ·
                ${a.page_count || 0} 页 · 发布 ${esc(a.pub_date || '未知')}</span>
            </div>
            <div class="toolbar">
              <a class="btn primary" href="#/album/${esc(a.album_id)}">进入详情（看章节）</a>
              <button class="btn" id="jm-dl">下载到本地</button>
            </div>
          `);
          const dl = document.getElementById('jm-dl');
          dl.onclick = async (ev) => {
            ev.target.disabled = true;
            ev.target.textContent = '已加入队列';
            try {
              await apiPost('/api/download', { album_id: a.album_id });
              toast('已加入下载队列，进度见「下载」页');
            } catch (err) {
              toast(`失败：${err.message}`);
              ev.target.disabled = false;
              ev.target.textContent = '下载到本地';
            }
          };
          return;
        }
        jmMiss = q;
      } catch (err) {
        // 接口/网络故障：不能说成"车号不存在"，那会误导用户
        jmError = err.message;
      }
    }

    setMain('<div class="loading">搜索中…</div>');
    const url = `/api/search?q=${encodeURIComponent(q)}&by=${by}&order=${order}&time=${time}&page=${page}`;
    const data = await apiGet(url);

    const mk = (p) => `#/search?q=${encodeURIComponent(q)}&by=${by}&order=${order}&time=${time}&page=${p}`;
    const sel = (name, opts, cur) => `<select id="${name}">${
      opts.map(([v, label]) => `<option value="${v}"${v === cur ? ' selected' : ''}>${label}</option>`).join('')
    }</select>`;

    const notice = jmError
      ? `<div class="notice warn">JM 号 <b>${esc(q)}</b> 查询失败：${esc(jmError)}<br>
           下面是按关键词「${esc(q)}」搜到的结果。</div>`
      : (jmMiss
        ? `<div class="notice warn">没有找到 JM 号 <b>${esc(jmMiss)}</b> ——
             可能是车号有误，也可能这部漫画<b>仅登录用户可见</b>。<br>
             下面是按关键词「${esc(jmMiss)}」搜到的结果。</div>`
        : '');

    setMain(`
      <h1 class="page-title">搜索：${esc(q)} <small>${esc(by)} · 命中 ${data.total}</small></h1>
      ${notice}
      <div class="toolbar">
        ${sel('f-by', [['site', '关键词 / JM 号'], ['tag', '标签'], ['author', '作者'], ['work', '作品'], ['actor', '人物']], by)}
        ${sel('f-order', [['mr', '最新'], ['mv', '最多观看'], ['mp', '最多图片'], ['tf', '最多爱心'], ['tr', '评分'], ['md', '评论']], order)}
        ${sel('f-time', [['a', '全部时间'], ['t', '今日'], ['w', '本周'], ['m', '本月']], time)}
      </div>
      ${comicList(data.items, { numbered: true })}
      ${pager(page, data.has_next, mk)}
    `);

    const go = () => {
      location.hash = `#/search?q=${encodeURIComponent(q)}&by=${document.getElementById('f-by').value}`
        + `&order=${document.getElementById('f-order').value}&time=${document.getElementById('f-time').value}&page=1`;
    };
    ['f-by', 'f-order', 'f-time'].forEach((id) => { document.getElementById(id).onchange = go; });
  }

  async function viewDiscover() {
    setMain('<div class="loading">加载中…</div>');
    const data = await apiGet('/api/categories');

    const catOptions = (data.categories || [])
      .map((c) => `<option value="${esc(c.slug || '0')}">${esc(c.name)}（${c.total}）</option>`).join('');

    const blocks = (data.blocks || []).map((b) => `
      <div class="card">
        <h3>${esc(b.title)}</h3>
        <div>${(b.tags || []).map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('')}</div>
      </div>`).join('');

    setMain(`
      <h1 class="page-title">发现</h1>
      <div class="toolbar">
        <select id="d-cat">${catOptions}</select>
        <select id="d-order">
          <option value="mr">最新</option>
          <option value="mv_w" selected>周排行</option>
          <option value="mv_m">月排行</option>
          <option value="mv_t">日排行</option>
          <option value="mv">总排行</option>
          <option value="tf">最多爱心</option>
          <option value="mp">最多图片</option>
        </select>
      </div>
      <div id="d-list"><div class="loading">加载中…</div></div>
      <h1 class="page-title" style="margin-top:24px">标签云</h1>
      <div class="grid2">${blocks}</div>
    `);

    const load = async (page = 1) => {
      const cat = document.getElementById('d-cat').value;
      const order = document.getElementById('d-order').value;
      const list = document.getElementById('d-list');
      list.innerHTML = '<div class="loading">加载中…</div>';
      const res = await apiGet(`/api/rankings?category=${encodeURIComponent(cat)}&order=${order}&page=${page}`);
      list.innerHTML = comicList(res.items, { numbered: true }) + `<div class="pager">
        ${page > 1 ? `<button class="btn" data-goto="${page - 1}">上一页</button>` : ''}
        <span>第 ${page} 页</span>
        ${res.has_next ? `<button class="btn" data-goto="${page + 1}">下一页</button>` : ''}
      </div>`;
      list.querySelectorAll('[data-goto]').forEach((btn) => {
        btn.onclick = () => load(parseInt(btn.dataset.goto, 10));
      });
    };
    document.getElementById('d-cat').onchange = () => load(1);
    document.getElementById('d-order').onchange = () => load(1);
    await load(1);
  }

  async function viewAlbum(albumId) {
    setMain('<div class="loading">加载中…</div>');
    const [a, hist] = await Promise.all([
      apiGet(`/api/album/${encodeURIComponent(albumId)}`),
      apiGet(`/api/history/${encodeURIComponent(albumId)}`).catch(() => ({ item: null })),
    ]);

    const tags = (a.tags || []).map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('');
    const authors = (a.authors || []).join(', ');
    const chapters = (a.chapters || []).map((ch) => `
      <a class="chapter-row" href="#/read/${esc(ch.chapter_id)}?album=${esc(a.album_id)}">
        <span class="n">${ch.index}</span>
        <span class="t">${esc(ch.title)}</span>
        <span class="badge">›</span>
      </a>`).join('');

    const resume = hist.item && hist.item.chapter_id
      ? `<a class="btn primary" href="#/read/${esc(hist.item.chapter_id)}?album=${esc(a.album_id)}&page=${hist.item.page_index || 1}">
           继续阅读（第 ${hist.item.chapter_index || 1} 话 · 第 ${hist.item.page_index || 1} 页）</a>`
      : '';

    setMain(`
      <div class="detail-head">
        <div class="cover"><img src="/api/cover/${esc(a.album_id)}" alt="" referrerpolicy="no-referrer"></div>
        <div class="info">
          <h1>${esc(a.title)}</h1>
          <div class="kv">作者：${esc(authors || '未知')}</div>
          <div class="kv">JM${esc(a.album_id)} · ${a.page_count} 页 · ${a.chapters.length} 话</div>
          <div class="kv">发布 ${esc(a.pub_date || '-')} · 更新 ${esc(a.update_date || '-')}</div>
          <div class="kv">观看 ${esc(a.views || '-')} · 点赞 ${esc(a.likes || '-')}</div>
          <div class="tags" style="margin-top:8px">${tags}</div>
          <div class="detail-desc">${esc(a.description || '')}</div>
          <div class="toolbar" style="margin-top:12px">
            ${resume}
            <button class="btn" id="dl">下载到本地</button>
            <a class="btn ghost" href="https://18comic.vip/album/${esc(a.album_id)}/" target="_blank" rel="noreferrer noopener">原站</a>
          </div>
        </div>
      </div>
      <h1 class="page-title">章节 <small>${a.chapters.length} 话</small></h1>
      <div class="chapter-list">${chapters || '<div class="empty">没有章节</div>'}</div>
    `);

    document.getElementById('dl').onclick = async (ev) => {
      ev.target.disabled = true;
      ev.target.textContent = '已加入队列';
      try {
        await apiPost('/api/download', { album_id: a.album_id });
        toast('已加入下载队列，进度见「下载」页');
      } catch (err) {
        toast(`失败：${err.message}`);
        ev.target.disabled = false;
        ev.target.textContent = '下载到本地';
      }
    };
  }

  // ------------------------------------------------------------ 阅读器

  const READER_MODE_KEY = 'jmreader.reader.mode';

  async function viewReader(chapterId, params) {
    const albumId = params.get('album') || '';
    const startPage = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const isLocal = params.get('local') === '1';
    const albumTitle = params.get('albumTitle') || '';
    const chapterTitle = params.get('chapterTitle') || '';

    setMain('<div class="loading">加载中…</div>');

    let pages = [];
    let title = chapterTitle;
    try {
      if (isLocal && albumId) {
        const lib = await apiGet(`/api/library/${encodeURIComponent(albumId)}`);
        const ch = (lib.chapters || []).find((c) => String(c.chapter_id) === String(chapterId));
        if (!ch) throw new Error('本地没有这一话');
        pages = ch.pages || [];
        title = title || ch.title || '';
      } else {
        const info = await apiGet(`/api/chapter/${encodeURIComponent(chapterId)}?album_id=${encodeURIComponent(albumId)}`);
        pages = info.pages || [];
        title = title || info.title || '';
      }
    } catch (err) { renderError(err); return; }

    if (!pages.length) { setMain('<div class="empty">这一话没有图片</div>'); return; }

    let mode = localStorage.getItem(READER_MODE_KEY) || 'strip';

    const build = () => {
      window.onscroll = null;   // 换模式时先解掉上一个模式的滚动监听
      const body = mode === 'strip'
        ? `<div class="strip" id="strip">${pages.map((src, i) => `
             <img loading="lazy" data-i="${i + 1}" src="${esc(src)}" alt="" referrerpolicy="no-referrer">`).join('')}</div>`
        : `<div class="paged"><img id="paged-img" src="${esc(pages[Math.min(startPage, pages.length) - 1])}" alt="" referrerpolicy="no-referrer"></div>`;

      const pagedBar = mode === 'paged'
        ? `<button class="btn" id="prev">上一页</button>
           <span class="pageinfo" id="pageinfo">${Math.min(startPage, pages.length)} / ${pages.length}</span>
           <button class="btn" id="next">下一页</button>`
        : `<span class="pageinfo" id="pageinfo">共 ${pages.length} 页</span>`;

      setMain(`
        <div class="reader">
          <div class="reader-bar">
            <button type="button" class="btn ghost" id="reader-back">← 返回</button>
            <span class="grow"></span>
            ${pagedBar}
            <span class="grow"></span>
            <button class="btn" id="mode">${mode === 'strip' ? '切换翻页' : '切换长图'}</button>
          </div>
          ${body}
          <div class="reader-foot">— 完 —</div>
        </div>
      `);

      document.getElementById('mode').onclick = () => {
        mode = mode === 'strip' ? 'paged' : 'strip';
        localStorage.setItem(READER_MODE_KEY, mode);
        build();
      };

      document.getElementById('reader-back').onclick = () => {
        const fallback = isLocal
          ? (albumId ? `#/library/${albumId}` : '#/library')
          : (albumId ? `#/album/${albumId}` : '#/');
        goBack(fallback);
      };

      if (mode === 'paged') {
        let cur = Math.min(startPage, pages.length);
        const img = document.getElementById('paged-img');
        const info = document.getElementById('pageinfo');
        const show = (n) => {
          cur = Math.min(Math.max(1, n), pages.length);
          img.src = pages[cur - 1];
          info.textContent = `${cur} / ${pages.length}`;
          saveProgress(cur);
        };
        document.getElementById('prev').onclick = () => show(cur - 1);
        document.getElementById('next').onclick = () => show(cur + 1);
        img.onclick = (ev) => {
          const half = img.getBoundingClientRect().width / 2;
          show(ev.offsetX < half ? cur - 1 : cur + 1);
        };
        const onKey = (ev) => {
          if (ev.key === 'ArrowLeft') show(cur - 1);
          if (ev.key === 'ArrowRight') show(cur + 1);
        };
        document.addEventListener('keydown', onKey);
        onCleanup(() => document.removeEventListener('keydown', onKey));
        saveProgress(cur);
      } else {
        const strip = document.getElementById('strip');
        if (startPage > 1) {
          const target = strip.querySelector(`img[data-i="${startPage}"]`);
          if (target) setTimeout(() => target.scrollIntoView({ block: 'start' }), 60);
        }
        let saveTimer = 0;
        window.onscroll = () => {
          clearTimeout(saveTimer);
          saveTimer = setTimeout(() => {
            const imgs = strip.querySelectorAll('img');
            let current = 1;
            for (const im of imgs) {
              if (im.getBoundingClientRect().top <= window.innerHeight * 0.4) current = Number(im.dataset.i);
              else break;
            }
            saveProgress(current);
          }, 350);
        };
        saveProgress(startPage);
      }
    };

    function saveProgress(pageIndex) {
      if (!albumId) return;
      apiPost('/api/history', {
        album_id: albumId,
        album_title: albumTitle,
        cover_url: albumId ? `/api/cover/${albumId}` : '',
        chapter_id: chapterId,
        chapter_title: title,
        chapter_index: 0,
        page_index: pageIndex,
        page_count: pages.length,
      }).catch(() => {});
    }

    build();
  }

  // ------------------------------------------------------------ 收藏 / 历史 / 本地库

  async function viewFavorites(params) {
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const folder = params.get('folder') || '0';
    setMain('<div class="loading">加载中…</div>');
    try {
      const data = await apiGet(`/api/favorites?page=${page}&folder_id=${encodeURIComponent(folder)}`);
      const folders = (data.folders || []).map((f) =>
        `<option value="${esc(f.id)}"${String(f.id) === String(folder) ? ' selected' : ''}>${esc(f.name)}</option>`).join('');
      setMain(`
        <h1 class="page-title">我的收藏 <small>${data.total} 部</small></h1>
        ${folders ? `<div class="toolbar"><select id="f-folder">${folders}</select></div>` : ''}
        ${comicList(data.items, { numbered: true })}
        ${pager(page, data.has_next, (p) => `#/favorites?page=${p}&folder=${encodeURIComponent(folder)}`)}
      `);
      const sel = document.getElementById('f-folder');
      if (sel) sel.onchange = () => { location.hash = `#/favorites?folder=${encodeURIComponent(sel.value)}`; };
    } catch (err) {
      if (isAuthError(err)) {
        setMain(`<div class="empty">${
          String(err.message || '').includes('失效')
            ? '登录已失效，需要重新登录禁漫账号'
            : '收藏夹需要登录禁漫账号'
        }<br><br><a class="btn primary" href="#/settings">去登录</a></div>`);
        return;
      }
      throw err;
    }
  }

  async function viewHistory() {
    setMain('<div class="loading">加载中…</div>');
    const data = await apiGet('/api/history?limit=100');
    if (!data.items.length) {
      setMain('<div class="empty">还没有观看记录</div>');
      return;
    }
    const rows = data.items.map((h) => `
      <a class="history-row" href="#/album/${esc(h.album_id)}">
        <span class="thumb"><img loading="lazy" src="${esc(h.cover_url || `/api/cover/${h.album_id}`)}" alt="" referrerpolicy="no-referrer"></span>
        <span class="info">
          <span class="title">${esc(h.album_title || `JM${h.album_id}`)}</span>
          <span class="meta">${h.chapter_title ? `${esc(h.chapter_title)} · ` : ''}看到第 ${h.page_index} 页${h.page_count ? ` / ${h.page_count}` : ''}</span>
          <span class="meta">${new Date((h.updated_at || 0) * 1000).toLocaleString()}</span>
        </span>
        <span class="btn" data-resume="${esc(h.chapter_id)}" data-album="${esc(h.album_id)}" data-page="${h.page_index || 1}">继续</span>
      </a>`).join('');

    setMain(`
      <h1 class="page-title">观看历史 <small>${data.count} 条</small></h1>
      <div class="toolbar"><button class="btn" id="clear">清空历史</button></div>
      <div class="history-box">${rows}</div>
    `);

    app.querySelectorAll('[data-resume]').forEach((btn) => {
      btn.onclick = (ev) => {
        ev.preventDefault();
        const { resume, album, page } = btn.dataset;
        location.hash = `#/read/${resume}?album=${album}&page=${page}`;
      };
    });
    document.getElementById('clear').onclick = async () => {
      if (!confirm('确定清空全部观看历史？')) return;
      await apiDel('/api/history');
      toast('已清空');
      viewHistory();
    };
  }

  async function viewLibrary(diag) {
    setMain('<div class="loading">加载中…</div>');
    const data = await apiGet('/api/library');
    const items = data.items.map((it) => ({
      album_id: it.album_id,
      title: it.title || `JM${it.album_id}`,
      author: it.author,
      // 本地库的 tags 来自我们自己写的 metadata.json，是真实标签
      tags: it.tags || [],
      adddate: `${it.chapter_count} 话 · ${((it.size_bytes || 0) / 1048576).toFixed(1)} MB`,
      cover: `/api/library/${it.album_id}/cover`,
    }));

    // 扫描诊断：磁盘数、写库数、回读数三个数字摆在一起，对不上就一眼看得出来
    let diagHtml = '';
    if (diag) {
      const ok = diag.total_after === diag.disk_count;
      const bad = (diag.details || []).filter((d) => !d.ok);
      diagHtml = `
        <div class="notice ${ok ? '' : 'warn'}"
             style="${ok ? 'background:transparent;border:1px solid var(--line);' : ''}">
          扫描结果：磁盘上 <b>${diag.disk_count}</b> 个 · 成功写库 <b>${diag.added}</b> 个
          · 跳过 <b>${diag.skipped}</b> 个 · 写完回读索引 <b>${diag.total_after}</b> 部
          ${ok ? '' : '<br><b>磁盘数和索引数对不上，问题就出在这一步。</b>'}
          <br><span style="color:var(--muted)">
            下载目录：<code>${esc(diag.download_dir)}</code><br>
            索引文件：<code>${esc(diag.db_path)}</code><br>
            日志模式：<code>${esc(diag.journal_mode || '?')}</code>
            ${(diag.journal_mode && diag.journal_mode !== 'WAL')
              ? '（这块盘撑不住 WAL 的共享内存，已自动退回更保守但可靠的模式）' : ''}
          </span>
          ${(diag.duplicate_album_ids || []).length ? `
            <div style="margin-top:8px">
              <b>发现重复的 JM 号：</b>
              <code>${(diag.duplicate_album_ids || []).map(esc).join(', ')}</code><br>
              JM 号是索引的主键，重复的目录会被合并成一条 —— 这就是为什么磁盘上更多、
              列表里更少。多半是同一个本子被下载/复制了两份。
            </div>` : ''}
          ${bad.length ? `
            <details style="margin-top:8px">
              <summary style="cursor:pointer">有 ${bad.length} 个没写进去，点开看原因</summary>
              <div style="margin-top:6px">${bad.map((d) => `
                ❌ <code>${esc(d.dir)}</code> → ${esc(d.reason || '未知原因')}<br>
                　 解析出的 album_id = <code>${esc(d.album_id || '(空)')}</code>
              `).join('')}</div>
            </details>` : ''}
        </div>`;
    }

    // 索引落后于磁盘时主动提示，而不是静悄悄显示空列表
    let lagHtml = '';
    if (!diag && data.disk_count > data.count) {
      lagHtml = `
        <div class="notice warn">
          磁盘上下载目录里有 <b>${data.disk_count}</b> 部，但索引里只有 <b>${data.count}</b> 部。
          索引落后了 —— 点上面的「重新扫描下载目录」重建一下就好。
        </div>`;
    }

    setMain(`
      <h1 class="page-title">本地已下载 <small>${data.count} 部</small></h1>
      <div class="toolbar">
        <button class="btn" id="rescan">重新扫描下载目录</button>
        <span class="kv">存放在 data/downloads/</span>
      </div>
      ${diagHtml}
      ${lagHtml}
      ${comicList(items, { href: (it) => `#/library/${it.album_id}` })}
    `);
    document.getElementById('rescan').onclick = async (ev) => {
      ev.target.disabled = true;
      try {
        const r = await apiPost('/api/library/rescan');
        toast(`扫描完成：磁盘 ${r.disk_count} 个 / 写库 ${r.added} 个 / 索引 ${r.total_after} 部`);
        await viewLibrary(r);          // 把诊断一起带回去，刷新后直接显示
      } catch (err) {
        toast(`扫描失败：${err.message}`);
        ev.target.disabled = false;
      }
    };
  }

  async function viewLibraryDetail(albumId) {
    setMain('<div class="loading">加载中…</div>');
    const a = await apiGet(`/api/library/${encodeURIComponent(albumId)}`);
    const chapters = (a.chapters || []).map((ch) => `
      <a class="chapter-row" href="#/read/${esc(ch.chapter_id)}?album=${esc(albumId)}&local=1&albumTitle=${encodeURIComponent(a.title || '')}&chapterTitle=${encodeURIComponent(ch.title || '')}">
        <span class="n">${ch.chapter_index}</span>
        <span class="t">${esc(ch.title || `第 ${ch.chapter_index} 话`)}</span>
        <span class="badge">${ch.page_count} 页 ›</span>
      </a>`).join('');

    setMain(`
      <div class="detail-head">
        <div class="cover"><img src="/api/library/${esc(albumId)}/cover" alt="" referrerpolicy="no-referrer"></div>
        <div class="info">
          <h1>${esc(a.title || `JM${albumId}`)}</h1>
          <div class="kv">作者：${esc(a.author || '未知')}</div>
          <div class="kv">${a.chapter_count} 话 · ${a.page_count} 页 · ${(a.size_bytes / 1048576).toFixed(1)} MB</div>
          <div class="tags">${(a.tags || []).map((t) => `<span class="tag static">${esc(t)}</span>`).join('')}</div>
          <div class="toolbar" style="margin-top:12px">
            <a class="btn" href="#/album/${esc(albumId)}">查看在线详情</a>
            <button class="btn" id="del">删除本地文件</button>
          </div>
        </div>
      </div>
      <h1 class="page-title">章节</h1>
      <div class="chapter-list">${chapters || '<div class="empty">没有章节</div>'}</div>
    `);

    document.getElementById('del').onclick = async () => {
      if (!confirm('确定删除这部漫画的本地文件？')) return;
      await apiDel(`/api/library/${encodeURIComponent(albumId)}`);
      toast('已删除');
      location.hash = '#/library';
    };
  }

  async function viewTasks() {
    setMain('<div class="loading">加载中…</div>');
    const data = await apiGet('/api/download/tasks');
    if (!data.tasks.length) {
      setMain('<div class="empty">没有下载任务</div>');
      return;
    }
    const rows = data.tasks.map((t) => {
      const pct = t.total_pages ? Math.round((t.done_pages / t.total_pages) * 100) : 0;
      const running = t.status === 'running' || t.status === 'queued';
      return `
        <div class="card" style="margin-bottom:10px">
          <h3>${esc(t.title || `JM${t.album_id}`)}</h3>
          <div class="kv">状态：${esc(t.status)} · ${esc(t.message || '')}</div>
          <div class="kv">章节 ${t.done_chapters}/${t.total_chapters} · 图片 ${t.done_pages}/${t.total_pages}</div>
          <div class="progress"><i style="width:${pct}%"></i></div>
          ${running ? `<div class="toolbar" style="margin-top:8px"><button class="btn" data-cancel="${esc(t.task_id)}">取消</button></div>` : ''}
        </div>`;
    }).join('');

    setMain(`
      <h1 class="page-title">下载任务</h1>
      <div class="toolbar">
        <button class="btn" id="refresh">刷新</button>
        <span class="kv">同一时刻只跑 1 个任务，页与页之间会留间隔 —— 请对站点温柔一点</span>
      </div>
      ${rows}
    `);
    document.getElementById('refresh').onclick = viewTasks;
    app.querySelectorAll('[data-cancel]').forEach((b) => {
      b.onclick = async () => {
        await apiDel(`/api/download/tasks/${b.dataset.cancel}`);
        toast('已请求取消');
        viewTasks();
      };
    });
    if (data.tasks.some((t) => t.status === 'running' || t.status === 'queued')) {
      setTimeout(() => {
        if (location.hash.startsWith('#/tasks')) viewTasks().catch(() => {});
      }, 4000);
    }
  }

  async function viewSettings() {
    setMain('<div class="loading">加载中…</div>');
    const [acc, st] = await Promise.all([apiGet('/api/account'), apiGet('/api/settings')]);
    const px = st.proxy || {};
    const up = st.upstream || {};
    const ac = st.access || {};
    const perSec = up.min_interval_seconds > 0 ? (1 / up.min_interval_seconds).toFixed(0) : '∞';

    // 说清楚这个代理值会不会跟着 Windows 的系统代理开关变 —— 这是最容易踩的坑
    const followsSystem = (px.source || '').includes('系统');
    const depNote = px.mode === 'off'
      ? '已强制直连，不走任何代理。'
      : (followsSystem
        ? `<b>注意：当前跟随 Windows 的系统代理。</b>系统代理一关，这里就变成直连了。
           想让本程序<b>不受系统代理开关影响</b>，请用下面的「手动指定」把地址填死。`
        : '这个值由配置文件 / 本页写死，<b>和 Windows 的系统代理开关无关</b> —— 关掉系统代理也照样走。');

    // 代理框和访问地址是两码事，容易被混为一谈，所以这里分开写清楚
    const lanUrls = ac.lan_urls || [];
    const lanBlock = ac.open_to_lan
      ? `${!ac.from_localhost && ac.current_url
           ? `<div class="notice" style="margin:8px 0 0;background:var(--accent-soft);border:1px solid var(--accent)">
                你现在就是通过 <code>${esc(ac.current_url)}</code> 打开这个页面的，
                别的机器用<b>这个地址</b>就行。
              </div>`
           : ''}
         <div class="kv" style="margin-top:8px">本机探测到的其它地址（仅供参考）：</div>
         ${lanUrls.length
           ? `<div class="kv">${lanUrls.map((u) => `<code>${esc(u)}</code>`).join('<br>')}</div>`
           : `<div class="kv" style="color:var(--muted)">（没探测到）</div>`}
         <div class="kv" style="margin-top:6px;color:var(--muted)">
           装了 ZeroTier / Radmin / Clash TUN 的机器会多出几个虚拟网卡地址，
           那些从别的设备连不上。拿不准就在目标机器上逐个试，能打开的就是对的。
         </div>
         <div class="notice warn" style="margin:10px 0 0">
           这个服务<b>没有任何登录验证</b>。能连上这个地址的人都能看你的收藏夹、观看历史，
           还能下载漫画。只在家里内网用，<b>千万别映射到公网</b>。
         </div>`
      : `<div class="kv">当前只监听 <code>127.0.0.1</code>，也就是<b>只有本机</b>能访问。</div>
         <div class="kv" style="margin-top:6px">
           想让别的机器（手机、笔记本）也能用：把 <code>.env</code> 里的
           <code>JMREADER_HOST</code> 改成 <code>0.0.0.0</code>，然后重启本程序。
         </div>
         <div class="kv" style="margin-top:6px;color:var(--muted)">
           注意 <code>0.0.0.0</code> 是"监听所有网卡"的意思，只能填在
           <code>JMREADER_HOST</code> 里，<b>不能</b>填到上面的代理框。
         </div>`;

    setMain(`
      <h1 class="page-title">设置</h1>

      <div class="card">
        <h3>禁漫账号</h3>
        ${acc.logged_in
          ? `<div class="kv">已登录：<b>${esc(acc.username || '')}</b>${
                acc.login_at
                  ? ` <span style="color:var(--muted)">（登录于 ${fmtTime(acc.login_at)}）</span>`
                  : ''
             }</div>
             ${acc.expired
               ? `<div class="notice warn" style="margin:8px 0 0">
                    登录已失效${acc.invalid_since ? `（${fmtTime(acc.invalid_since)} 检测到）` : ''}。
                    ${acc.auto_relogin
                      ? (acc.relogin_blocked
                          ? '自动重新登录已连续失败多次，已停止尝试 —— 通常是密码改过了，请手动重新登录。'
                          : '正在尝试自动重新登录…')
                      : '禁漫的登录状态有效期为几小时，过期后需要重新登录。'}
                  </div>${acc.last_error ? `<div class="kv" style="color:var(--muted)">原因：${esc(acc.last_error)}</div>` : ''}`
               : ''}
             <div class="toolbar" style="margin-top:10px"><button class="btn" id="logout">退出登录</button></div>`
          : `<div class="kv">登录后可以读取你的收藏夹。登录凭据只保存在本地 data/ 目录，不会上传。</div>
             <div class="toolbar" style="margin-top:10px">
               <input id="u" placeholder="用户名" autocomplete="username" style="padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)">
               <input id="p" type="password" placeholder="密码" autocomplete="current-password" style="padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)">
               <button class="btn primary" id="login">登录</button>
             </div>
             <label class="kv" style="display:flex;align-items:center;gap:6px;margin-top:10px;cursor:pointer">
               <input type="checkbox" id="remember-inline"${acc.auto_relogin ? ' checked' : ''}
                      style="width:auto;margin:0">
               记住密码，登录失效后自动重新登录
             </label>
             ${rememberNote(acc)}`}
        ${acc.logged_in ? `
          <div style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px">
            <label class="kv" style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="remember"${acc.auto_relogin ? ' checked' : ''}
                     style="width:auto;margin:0">
              记住密码，登录失效后自动重新登录
            </label>
            <div class="kv" id="remember-result" style="margin-top:6px"></div>
            ${rememberNote(acc)}
          </div>` : ''}
      </div>

      <div class="card" style="margin-top:12px">
        <h3>网络代理</h3>
        <div class="kv">
          直连禁漫<b>不稳定</b>：实测连续 5 次只有 2 次能通，其余是证书校验失败或连接被重置。
          挂上本地代理（Clash / v2ray 等）则稳定。
        </div>
        <div class="kv" style="margin-top:6px">
          若这台机器是 NAS / 以服务方式运行，「跟随系统」常常探测不到代理
          —— 那就用<b>手动指定</b>填死地址，最稳。
        </div>
        <div class="toolbar" style="margin-top:10px">
          <select id="px-mode">
            <option value="auto"${px.mode === 'auto' ? ' selected' : ''}>跟随系统 / 配置文件</option>
            <option value="off"${px.mode === 'off' ? ' selected' : ''}>不使用代理（直连）</option>
            <option value="custom"${px.mode === 'custom' ? ' selected' : ''}>手动指定代理</option>
          </select>
          <input id="px-url" placeholder="http://127.0.0.1:7890" value="${esc(px.url || '')}"
                 autocomplete="off" spellcheck="false"
                 style="width:230px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)">
          <button class="btn primary" id="px-save">保存</button>
          <button class="btn" id="px-test">测试连接</button>
        </div>
        <div class="kv" id="px-result"></div>
        <div class="kv" id="px-effective">
          当前生效：<code>${esc(px.mode === 'off' ? '直连（不走代理）' : (px.proxy || '直连（系统探测不到代理）'))}</code>
          <span style="color:var(--muted)">（来源：${esc(px.source || '-')}）</span>
        </div>
        <div class="notice ${followsSystem ? 'warn' : ''}" id="px-dept"
             style="margin:8px 0 0;${followsSystem ? '' : 'background:transparent;border:1px solid var(--line);'}">
          ${depNote}
        </div>
        <div class="kv" style="margin-top:6px">
          常见值：Clash <code>http://127.0.0.1:7890</code> ／ v2rayN <code>http://127.0.0.1:10809</code>。
          「不使用代理」一般只用来排查问题。
        </div>
        <div class="notice warn" style="margin:10px 0 0">
          这里填的是<b>本程序连出去时走哪个代理</b>，不是给别人访问用的地址。
          JMReader 和 Clash 在同一台机器上就填 <code>127.0.0.1:7890</code>，
          <b>不要填 <code>0.0.0.0</code></b> —— 那是"监听所有网卡"的意思，连不过去。
          想让别的机器访问，看下面的「访问地址」。
        </div>
      </div>

      <div class="card" style="margin-top:12px">
        <h3>访问地址</h3>
        <div class="kv">本机：<code>${esc(ac.local_url || '-')}</code>
          <span style="color:var(--muted)">（监听 ${esc(ac.host || '-')}:${ac.port || '-'}）</span>
        </div>
        ${lanBlock}
      </div>

      <div class="card" style="margin-top:12px">
        <h3>对站点的负担</h3>
        <div class="kv">本次运行已向禁漫发出 <b>${up.upstream_requests || 0}</b> 次请求，运行了 ${up.uptime_seconds || 0} 秒。</div>
        <div class="kv">全局限速：两次上游请求至少间隔 ${up.min_interval_seconds} 秒（最多 ${perSec} 请求/秒），
          无论多少并发都不会突破。</div>
        <div class="kv">列表、详情、封面、章节图全都带缓存，重复访问不会再回源。</div>
      </div>

      <div class="card" style="margin-top:12px">
        <h3>关于</h3>
        <div class="kv">JMReader —— 禁漫天堂第三方阅读器，漫画 only、无广告。</div>
        <div class="kv">数据目录：<code>data/</code>（Cookie、历史数据库、下载的漫画）</div>
        <div class="kv">接口文档：<a href="/docs" target="_blank">/docs</a></div>
      </div>
    `);

    const loginBtn = document.getElementById('login');
    if (loginBtn) {
      loginBtn.onclick = async () => {
        const username = document.getElementById('u').value.trim();
        const password = document.getElementById('p').value;
        if (!username || !password) { toast('请填写用户名和密码'); return; }
        const rememberBox = document.getElementById('remember-inline');
        loginBtn.disabled = true;
        try {
          await apiPost('/api/login', {
            username, password, remember: !!(rememberBox && rememberBox.checked),
          });
          toast(rememberBox && rememberBox.checked ? '登录成功，已记住密码' : '登录成功');
          viewSettings();
        } catch (err) {
          toast(`登录失败：${err.message}`);
          loginBtn.disabled = false;
        }
      };
    }
    const logoutBtn = document.getElementById('logout');
    if (logoutBtn) {
      logoutBtn.onclick = async () => {
        await apiPost('/api/logout');
        toast('已退出，保存的密码也一并清除');
        viewSettings();
      };
    }
    const rememberBox = document.getElementById('remember');
    if (rememberBox) {
      rememberBox.onchange = async () => {
        const out = document.getElementById('remember-result');
        try {
          const r = await apiPost('/api/remember', { enabled: rememberBox.checked });
          out.innerHTML = r.enabled
            ? '<span style="color:var(--accent)">已开启。会话过期后会尝试自动重新登录。</span>'
            : '<span style="color:var(--muted)">已关闭，存下来的密码已经清除。</span>';
        } catch (err) {
          out.textContent = `保存失败：${err.message}`;
          rememberBox.checked = !rememberBox.checked;
        }
      };
    }

    // ---- 代理 ----
    const modeSel = document.getElementById('px-mode');
    const urlInput = document.getElementById('px-url');
    const pxResult = document.getElementById('px-result');
    const pxEffective = document.getElementById('px-effective');
    const pxDep = document.getElementById('px-dept');

    // 只有「手动指定」才需要填地址。用 readonly 而不是 disabled，这样值仍能读出来。
    const syncUrl = () => {
      const custom = modeSel.value === 'custom';
      urlInput.readOnly = !custom;
      urlInput.style.opacity = custom ? '1' : '.5';
    };
    syncUrl();
    modeSel.onchange = syncUrl;

    document.getElementById('px-save').onclick = async (ev) => {
      ev.target.disabled = true;
      pxResult.textContent = '保存中…';
      try {
        const r = await apiPost('/api/settings/proxy', { mode: modeSel.value, url: urlInput.value });
        const p = r.proxy;
        pxResult.textContent = '✅ 已保存。';
        pxEffective.innerHTML = `当前生效：<code>${
          esc(p.mode === 'off' ? '直连（不走代理）' : (p.proxy || '直连（系统探测不到代理）'))
        }</code> <span style="color:var(--muted)">（来源：${esc(p.source || '-')}）</span>`;
        // 提示条也要跟着更新，否则会显示上一个模式的说明
        const fs = (p.source || '').includes('系统');
        pxDep.className = `notice${fs ? ' warn' : ''}`;
        pxDep.style.cssText = `margin:8px 0 0;${fs ? '' : 'background:transparent;border:1px solid var(--line);'}`;
        pxDep.innerHTML = p.mode === 'off'
          ? '已强制直连，不走任何代理。'
          : (fs
            ? `<b>注意：当前跟随 Windows 的系统代理。</b>系统代理一关，这里就变成直连了。
               想让本程序<b>不受系统代理开关影响</b>，请用下面的「手动指定」把地址填死。`
            : '这个值由配置文件 / 本页写死，<b>和 Windows 的系统代理开关无关</b> —— 关掉系统代理也照样走。');
        toast('代理设置已保存，下次请求即生效');
      } catch (err) {
        pxResult.textContent = `❌ 保存失败：${err.message}`;
      }
      ev.target.disabled = false;
    };

    document.getElementById('px-test').onclick = async (ev) => {
      ev.target.disabled = true;
      pxResult.textContent = '正在测试…（要真的连一次禁漫，稍等）';
      try {
        const r = await apiPost('/api/settings/proxy/test');
        const usedProxy = (r.proxy_used || '').indexOf('http') === 0;
        // 失败 + 没走代理 = 十有八九是"跟随系统"而系统代理关着，直接点破
        const hint = (!r.ok && !usedProxy)
          ? `<br><b>这次没有走代理。</b>如果上面是「跟随系统」，说明系统代理是关的 ——
             要么去打开它，要么改用「手动指定」把 <code>http://127.0.0.1:7890</code> 填死
             （后者不受系统代理开关影响）。`
          : '';
        pxResult.innerHTML = `${r.ok ? '✅' : '❌'} ${esc(r.detail)}<br>
          <span style="color:var(--muted)">耗时 ${r.ms} ms · 实际使用：<code>${esc(r.proxy_used || '未知')}</code></span>${hint}`;
      } catch (err) {
        pxResult.textContent = `❌ 测试失败：${err.message}`;
      }
      ev.target.disabled = false;
    };
  }

  // ------------------------------------------------------------ 标签筛选

  const MAX_TAGS = 5;          // 与后端 TAG_SEARCH_MAX_TAGS 保持一致
  const TAG_EXPANDED_KEY = 'jmreader.tags.expanded';

  let tagSel = [];             // 当前选中的标签
  let tagExpanded = false;     // 是否展开全部标签
  let tagBlocks = [];          // 内置标签表

  /** 加入一个标签（可以不在那 47 个里 —— 筛选接受任意标签）。 */
  function addTag(raw) {
    const t = String(raw || '').trim();
    if (!t) return false;
    if (tagSel.includes(t)) { toast(`「${t}」已经选过了`); return false; }
    if (tagSel.length >= MAX_TAGS) { toast(`最多同时选 ${MAX_TAGS} 个标签`); return false; }
    tagSel.push(t);
    return true;
  }

  function tagPanelHtml() {
    // 收起时只展示第一组（禁漫的「主題A漫」），展开后是全部 4 组
    const shown = tagExpanded ? tagBlocks : tagBlocks.slice(0, 1);
    const total = tagBlocks.reduce((n, b) => n + (b.tags || []).length, 0);
    const groups = shown.map((b) => `
      <div class="group">
        <h4>${esc(b.title)}</h4>
        <div class="tagcloud">${(b.tags || []).map((t) => `
          <button type="button" class="pick${tagSel.includes(t) ? ' on' : ''}" data-pick="${esc(t)}">${esc(t)}</button>`).join('')}</div>
      </div>`).join('');

    // 已选标签一律在这里显示成可移除的 chip —— 这样从详情页点进来的
    // 长尾标签（不在那 47 个里）也能看见、也能取消
    const chips = tagSel.map((t) => `
      <button type="button" class="pick on" data-unpick="${esc(t)}" title="点击移除">${esc(t)} ×</button>`).join('');

    return `
      ${groups}
      <div class="foot">
        <button type="button" class="btn" id="tag-expand">${tagExpanded ? '收起' : `展开全部 ${total} 个标签`}</button>
        <span style="flex:1"></span>
        <button type="button" class="btn primary" id="tag-go">筛选</button>
        <button type="button" class="btn ghost" id="tag-clear">清空</button>
      </div>

      <div class="selrow">
        <span class="hint">已选 ${tagSel.length}/${MAX_TAGS}：</span>
        ${chips || '<span class="hint">（还没选，点上面的标签，或在下面输入）</span>'}
      </div>

      <div class="selrow">
        <input id="tag-input" type="text" autocomplete="off"
               placeholder="输入任意标签，例如 中出 / 強制口交 / 過膝襪，回车添加">
        <button type="button" class="btn" id="tag-add">添加</button>
      </div>

      <div class="tips">
        <b>关于标签，有几点要知道：</b>
        <ul>
          <li>上面罗列的是禁漫挑出来的<b>常用标签（共 ${total} 个）</b>，<b>不是全部标签</b>。
              但筛选<b>接受任意标签</b> —— 在下面输入框里打字回车就能加，不必是列表里的。</li>
          <li>最省事的找标签办法：打开任意一本漫画，<b>点它的标签</b>，就会自动带到这里。</li>
          <li>标签一律以<b>繁体</b>书写（是 <code>女僕</code> 不是 <code>女仆</code>）。
              多数简体输入禁漫会自动转换 —— 实测 <code>纯爱</code>/<code>純愛</code>、
              <code>触手</code>/<code>觸手</code>、<code>调教</code>/<code>調教</code> 结果完全一致；
              但<b>并非全部如此</b>（<code>女仆</code> 只有 149 条，<code>女僕</code> 有 4223 条）。
              <b>拿不准就用繁体</b>，或者干脆从漫画详情页点标签。</li>
          <li>标签之间是「同时具备」还是「任一即可」，由结果上方的下拉框决定。
              禁漫自己只支持单标签查询，多选是本地逐标签取页再求交集／并集，
              所以最多同时选 ${MAX_TAGS} 个，第一次筛选可能要等几秒。</li>
        </ul>
      </div>`;
  }

  function tagQuery(extra = {}) {
    const q = new URLSearchParams();
    if (tagSel.length) q.set('sel', tagSel.join(','));
    if (tagExpanded) q.set('all', '1');
    const merged = { mode: extra.mode || 'and', order: extra.order || 'mr', time: extra.time || 'a', ...extra };
    if (tagSel.length) {
      q.set('mode', merged.mode);
      q.set('order', merged.order);
      q.set('time', merged.time);
      if (merged.page > 1) q.set('page', String(merged.page));
    }
    return q.toString();
  }

  async function viewTags(params) {
    const selected = (params.get('sel') || '').split(',').map((s) => s.trim()).filter(Boolean);
    tagSel = selected.slice(0, MAX_TAGS);
    tagExpanded = params.get('all') === '1'
      || localStorage.getItem(TAG_EXPANDED_KEY) === '1';
    const mode = params.get('mode') || 'and';
    const order = params.get('order') || 'mr';
    const time = params.get('time') || 'a';
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);

    setMain('<div class="loading">加载中…</div>');
    tagBlocks = (await apiGet('/api/tags')).blocks || [];

    let resultHtml = '';
    if (tagSel.length) {
      // 多标签是逐个取页再本地做交集，首次可能要几秒；结果会缓存
      setMain('<div class="loading">正在筛选…多标签需要逐个取页做交集，首次可能要几秒</div>');
      const url = `/api/tag-search?tags=${encodeURIComponent(tagSel.join(','))}`
        + `&mode=${mode}&order=${order}&time=${time}&page=${page}`;
      const res = await apiGet(url);
      const mk = (p) => `#/tags?${tagQuery({ mode, order, time, page: p })}`;
      const approx = res.approximate
        ? `<span class="kv">（在各自前几页范围内计算，共 ${res.total} 部${res.has_next ? '，还有更多' : ''}）</span>`
        : '';
      resultHtml = `
        <h1 class="page-title">
          筛选结果
          <small>${mode === 'and' ? '同时具备' : '任一即可'} · ${tagSel.length} 个标签</small>
          ${approx}
        </h1>
        ${comicList(res.items, { numbered: true })}
        ${pager(page, res.has_next, mk)}`;
    } else {
      resultHtml = '<div class="empty">选好标签后点「筛选」</div>';
    }

    const sel = (name, opts, cur) => `<select id="${name}">${
      opts.map(([v, label]) => `<option value="${v}"${v === cur ? ' selected' : ''}>${label}</option>`).join('')
    }</select>`;

    setMain(`
      <h1 class="page-title">标签筛选 <small>点一下就选中，可以多选</small></h1>
      <div class="tagpanel" id="tagpanel-host">${tagPanelHtml()}</div>
      ${tagSel.length ? `<div class="toolbar">
        ${sel('g-mode', [['and', '同时具备全部标签'], ['or', '具备任一标签']], mode)}
        ${sel('g-order', [['mr', '最新'], ['mv', '最多观看'], ['mp', '最多图片'], ['tf', '最多爱心'], ['tr', '评分'], ['md', '评论']], order)}
        ${sel('g-time', [['a', '全部时间'], ['t', '今日'], ['w', '本周'], ['m', '本月']], time)}
      </div>` : ''}
      ${resultHtml}
    `);

    const host = document.getElementById('tagpanel-host');

    // 任何改动都整体重渲染面板：面板很小，这样「已选 chip」和按钮高亮永远一致
    function renderPanel(focusInput) {
      host.innerHTML = tagPanelHtml();
      bindPanel();
      if (focusInput) {
        const inp = document.getElementById('tag-input');
        if (inp) inp.focus();
      }
    }

    function bindPanel() {
      host.querySelectorAll('[data-pick]').forEach((btn) => {
        btn.onclick = () => {
          const t = btn.dataset.pick;
          const at = tagSel.indexOf(t);
          if (at >= 0) tagSel.splice(at, 1);
          else if (!addTag(t)) return;
          renderPanel(false);
        };
      });
      host.querySelectorAll('[data-unpick]').forEach((btn) => {
        btn.onclick = () => {
          const at = tagSel.indexOf(btn.dataset.unpick);
          if (at >= 0) tagSel.splice(at, 1);
          renderPanel(false);
        };
      });
      document.getElementById('tag-expand').onclick = () => {
        tagExpanded = !tagExpanded;
        localStorage.setItem(TAG_EXPANDED_KEY, tagExpanded ? '1' : '0');
        renderPanel(false);
      };
      document.getElementById('tag-clear').onclick = () => {
        tagSel = [];
        location.hash = '#/tags';
      };
      document.getElementById('tag-go').onclick = () => {
        if (!tagSel.length) { toast('请先选择标签'); return; }
        const q = tagQuery({ mode, order, time, page: 1 });
        location.hash = `#/tags?${q}`;
      };
      const input = document.getElementById('tag-input');
      const submit = () => {
        if (addTag(input.value)) renderPanel(true);
      };
      document.getElementById('tag-add').onclick = submit;
      input.onkeydown = (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); submit(); }
      };
    }
    bindPanel();

    if (tagSel.length) {
      ['g-mode', 'g-order', 'g-time'].forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.onchange = () => {
          location.hash = `#/tags?${tagQuery({
            mode: document.getElementById('g-mode').value,
            order: document.getElementById('g-order').value,
            time: document.getElementById('g-time').value,
            page: 1,
          })}`;
        };
      });
    }
  }

  // ------------------------------------------------------------ 路由

  async function route() {
    window.onscroll = null;
    cleanups.forEach((fn) => { try { fn(); } catch { /* 忽略 */ } });
    cleanups = [];
    const hash = currentHash();
    syncNavStack(hash);
    updateBackButton(hash);
    const path = hash.split('?')[0].replace(/^#/, '');
    const params = parseQuery(hash);
    const parts = path.split('/').filter(Boolean);

    const q = document.getElementById('search-q');
    const by = document.getElementById('search-by');
    if (parts[0] === 'search') {
      q.value = params.get('q') || '';
      by.value = params.get('by') || 'site';
    }

    app.scrollTop = 0;
    window.scrollTo(0, 0);

    try {
      if (parts.length === 0) return await viewHome();
      switch (parts[0]) {
        case 'search': return await viewSearch(params);
        case 'tags': return await viewTags(params);
        case 'discover': return await viewDiscover();
        case 'album': return await viewAlbum(parts[1]);
        case 'read': return await viewReader(parts[1], params);
        case 'favorites': return await viewFavorites(params);
        case 'history': return await viewHistory();
        case 'library':
          return parts[1] ? await viewLibraryDetail(parts[1]) : await viewLibrary();
        case 'tasks': return await viewTasks();
        case 'settings': return await viewSettings();
        default: setMain('<div class="empty">页面不存在</div>');
      }
    } catch (err) {
      renderError(err);
    }
  }

  document.getElementById('search-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const value = document.getElementById('search-q').value.trim();
    if (!value) return;
    location.hash = `#/search?by=${document.getElementById('search-by').value}&q=${encodeURIComponent(value)}`;
  });

  window.addEventListener('hashchange', route);
  route();
})();
