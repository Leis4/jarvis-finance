/* JARVIS personal finance UI. Vanilla, CSP-safe, no external requests. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const el = (tag, attrs = {}, ...kids) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  };
  // CSP: inline style attributes are blocked, CSSOM property writes are allowed.
  const styled = (node, styles, ...kids) => {
    for (const [key, value] of Object.entries(styles)) node.style.setProperty(key, value);
    if (kids.length) node.append(...kids.flat().filter((kid) => kid !== null && kid !== undefined && kid !== false));
    return node;
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const icon = (name) => {
    const svg = document.createElementNS(SVG_NS, 'svg');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.append(use);
    return svg;
  };
  const plural = (count, one, few, many) => {
    const value = Math.abs(Number(count) || 0) % 100;
    const last = value % 10;
    if (value > 10 && value < 20) return `${count} ${many}`;
    if (last === 1) return `${count} ${one}`;
    if (last >= 2 && last <= 4) return `${count} ${few}`;
    return `${count} ${many}`;
  };

  const state = {
    csrf: null,
    webOnly: false,
    data: {},
    history: { items: [], offset: 0, total: 0, categories: [], sources: [] },
    filters: { kind: 'all', confirmation: 'all', category: '', days: 90, q: '' },
    chartDays: 30,
    coinMode: 'ai',
    coinThread: [],
    catalog: null,
    pendingGroups: null,
    loaded: {},
    chat: { sessions: [], sessionId: null, messages: [], streaming: false, status: null, controller: null, searchTimer: null },
  };

  /* ------------------------------ formatting ------------------------------ */
  const NBSP = '\u00a0';
  const fmtNumber = (value, digits = 2) => {
    const num = Number(value);
    if (value === null || value === undefined || value === '' || !Number.isFinite(num)) return null;
    return new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: digits }).format(num);
  };
  const money = (value, opts = {}) => {
    const text = fmtNumber(value, opts.digits ?? 2);
    if (text === null) return '—';
    const num = Number(value);
    const sign = opts.sign && num > 0 ? '+' : '';
    return sign + text + NBSP + '₽';
  };
  const compact = (value) => {
    const num = Number(value);
    if (!Number.isFinite(num)) return '—';
    const abs = Math.abs(num);
    if (abs >= 1e6) return (num / 1e6).toFixed(1).replace('.', ',') + ' млн';
    if (abs >= 1e4) return Math.round(num / 1e3) + ' тыс';
    return fmtNumber(num, 0) ?? '—';
  };
  const dateShort = (iso) => {
    if (!iso) return '—';
    const date = new Date(String(iso).length <= 10 ? iso + 'T00:00:00' : iso);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(date);
  };
  const dateFull = (iso) => {
    if (!iso) return '—';
    const date = new Date(String(iso).length <= 10 ? iso + 'T00:00:00' : iso);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' }).format(date);
  };
  const ago = (iso) => {
    if (!iso) return 'нет данных';
    const then = new Date(iso).getTime();
    if (!Number.isFinite(then)) return 'нет данных';
    const minutes = Math.round((Date.now() - then) / 60000);
    if (minutes < 1) return 'только что';
    if (minutes < 60) return `${minutes} мин назад`;
    const hours = Math.round(minutes / 60);
    if (hours < 36) return `${hours} ч назад`;
    const days = Math.round(hours / 24);
    if (days < 45) return `${days} дн назад`;
    return dateFull(iso);
  };
  const signed = (value) => (Number(value) > 0 ? 'in' : '');
  const pct = (part, total) => {
    const a = Number(part), b = Number(total);
    if (!Number.isFinite(a) || !Number.isFinite(b) || b <= 0) return 0;
    return Math.max(0, Math.min(100, (a / b) * 100));
  };
  const escapeHtml = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const CONFIDENCE_LABELS = {
    source_bound: 'по данным',
    measured: 'измерено',
    estimated: 'оценка',
    hypothesis: 'гипотеза',
    assumption: 'предположение',
    low: 'низкая уверенность',
    medium: 'средняя уверенность',
    high: 'высокая уверенность',
  };
  const confidenceLabel = (event) => {
    const raw = String(event.confidence || event.epistemic || '').toLowerCase();
    return CONFIDENCE_LABELS[raw] || 'наблюдение';
  };
  const REASON_LABELS = {
    new_merchant: 'новый продавец',
    many_similar: 'много похожих',
    unknown: 'без категории',
    needs_review: 'нужна проверка',
    conflict: 'возможный перевод',
    marketplace: 'маркетплейс',
    recurring: 'повторяется',
    installment: 'рассрочка',
  };
  const reasonLabel = (reason) => REASON_LABELS[reason] || String(reason || '').replace(/_/g, ' ');
  const COVERAGE_LABELS = { partial: 'неполная', complete: 'полная', unknown: 'неизвестна', none: 'нет' };
  const coverageLabel = (value) => COVERAGE_LABELS[String(value || '').toLowerCase()] || String(value || '—');
  const SYNC_LABELS = {
    auth_required: 'нужен PIN', updated: 'обновлено', unavailable: 'недоступен',
    running: 'выполняется', busy: 'уже идёт', verified: 'проверено', delivered: 'отправлено',
  };
  const syncLabel = (value) => SYNC_LABELS[String(value || '').toLowerCase()] || String(value || '—');

  const titleFor = (category) => {    const titles = state.catalog?.titles || {};
    if (titles[category]) return titles[category];
    const fallback = {
      food: 'Питание', peer_transfer_out: 'Перевод человеку', games: 'Игры',
      other: 'Прочее', salary: 'Зарплата', stipend: 'Стипендия',
      family_income: 'Поддержка семьи', internal_transfer: 'Между счетами',
      unknown: 'Без категории', uncategorized: 'Без категории', pending_review: 'Нужна проверка',
    };
    if (fallback[category]) return fallback[category];
    return String(category || 'Без категории').replace(/_/g, ' ');
  };

  // One direction-aware category set for every classification surface: the owner
  // never sees "перевод от друга" on an outgoing operation or vice versa.
  function pickerGroups(income) {
    const want = income ? 'in' : 'out';
    const merged = {};
    const categories = state.catalog?.categories;
    if (Array.isArray(categories) && categories.length) {
      categories
        .filter((item) => item.direction === 'any' || item.direction === want)
        .forEach((item) => {
          if (!merged[item.bucket]) merged[item.bucket] = [];
          merged[item.bucket].push(item);
        });
      return merged;
    }
    const sources = income
      ? [state.catalog?.income_groups]
      : [state.catalog?.expense_groups, state.catalog?.transfer_groups];
    sources.forEach((source) => {
      Object.entries(source || {}).forEach(([bucket, items]) => {
        merged[bucket] = [...(merged[bucket] || []), ...items];
      });
    });
    return merged;
  }

  /* -------------------------------- api ---------------------------------- */
  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.status = status;
      this.payload = payload;
    }
  }

  async function csrfToken() {
    if (state.csrf) return state.csrf;
    const response = await fetch('/auth/csrf', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new ApiError('Сессия истекла', response.status);
    const data = await response.json();
    state.csrf = data.csrf;
    state.webOnly = Boolean(data.web_only);
    return state.csrf;
  }

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    const headers = {};
    if (method !== 'GET') {
      headers['content-type'] = 'application/json';
      headers['x-owner-csrf'] = await csrfToken();
    }
    const response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = { detail: text }; }
    if (response.status === 403 && !path.startsWith('/auth')) {
      location.replace('/');
      throw new ApiError('Нужен вход владельца', 403, payload);
    }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.reason);
      throw new ApiError(typeof detail === 'string' ? detail : `Ошибка ${response.status}`, response.status, payload);
    }
    return payload;
  }

  /* ------------------------------- toasts -------------------------------- */
  function toast(message, kind = '') {
    const node = el('div', { class: `toast ${kind}`.trim() }, message);
    $('#toasts').append(node);
    setTimeout(() => node.remove(), 4200);
  }

  function banner(text, kind = 'warn', action = null) {
    const node = $('#banner');
    if (!text) { node.hidden = true; node.replaceChildren(); return; }
    if (sessionStorage.getItem('jarvis.banner.dismissed') === text) { node.hidden = true; return; }
    node.className = `banner ${kind}`;
    const parts = [el('span', {}, text)];
    if (action) parts.push(el('button', { type: 'button', onclick: action.run }, action.label));
    parts.push(el('button', {
      class: 'banner-close', type: 'button', 'aria-label': 'Скрыть сообщение',
      onclick: () => { sessionStorage.setItem('jarvis.banner.dismissed', text); node.hidden = true; },
    }, '×'));
    node.replaceChildren(...parts);
    node.hidden = false;
  }

  /* --------------------------- hero highlight ---------------------------- */
  function bindHeroGlow() {
    // Delegated so it works for any dashboard re-render, and it self-disables
    // on touch, reduced motion or when the owner turns motion off.
    let frame = 0;
    let pending = null;
    document.addEventListener('pointermove', (event) => {
      if (document.documentElement.dataset.motion === 'off') return;
      if (window.matchMedia('(hover: none)').matches) return;
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const card = event.target instanceof Element ? event.target.closest('.card.hero') : null;
      const glow = card && card.querySelector('.hero-glow');
      if (!glow) return;
      const rect = card.getBoundingClientRect();
      pending = { glow, x: event.clientX - rect.left, y: event.clientY - rect.top };
      glow.classList.add('on');
      if (!frame) {
        frame = requestAnimationFrame(() => {
          frame = 0;
          if (!pending) return;
          pending.glow.style.setProperty('--gx', `${pending.x}px`);
          pending.glow.style.setProperty('--gy', `${pending.y}px`);
          pending = null;
        });
      }
    }, { passive: true });
    document.addEventListener('pointerout', (event) => {
      const card = event.target instanceof Element ? event.target.closest('.card.hero') : null;
      if (!card) return;
      const next = event.relatedTarget;
      if (!(next instanceof Node) || !card.contains(next)) {
        card.querySelector('.hero-glow')?.classList.remove('on');
      }
    }, { passive: true });
  }

  /* ------------------------------- charts -------------------------------- */
  function spendChart(host, series, options = {}) {
    host.replaceChildren();
    const tip = el('div', { class: 'chart-tip', id: 'chart-tip' });
    tip.hidden = true;
    host.append(tip);
    if (!series.length) {
      host.append(el('p', { class: 'chart-empty' }, 'Нет данных за период'));
      return;
    }
    const width = 640, height = options.height || 158, pad = 8;
    const values = series.map((point) => Number(point.value) || 0);
    const max = Math.max(...values, 1);
    const x = (i) => pad + (i * (width - pad * 2)) / Math.max(series.length - 1, 1);
    const y = (v) => height - pad - (v / max) * (height - pad * 2);
    const line = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    const area = `${line} L${x(values.length - 1).toFixed(1)},${height - pad} L${x(0).toFixed(1)},${height - pad} Z`;
    const color = options.color || '#3ddc97';
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', options.label || 'График расходов');
    svg.innerHTML = `
      <defs>
        <linearGradient id="fill-${options.id || 'a'}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${color}" stop-opacity=".28"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${[0.25, 0.5, 0.75].map((part) =>
        `<line x1="${pad}" y1="${(height - pad - part * (height - pad * 2)).toFixed(1)}"
               x2="${width - pad}" y2="${(height - pad - part * (height - pad * 2)).toFixed(1)}"
               stroke="rgba(255,255,255,.05)" stroke-width="1"/>`).join('')}
      <line x1="${pad}" y1="${y(values.reduce((sum, v) => sum + v, 0) / Math.max(values.length, 1)).toFixed(1)}"
            x2="${width - pad}" y2="${y(values.reduce((sum, v) => sum + v, 0) / Math.max(values.length, 1)).toFixed(1)}"
            stroke="${color}" stroke-opacity=".22" stroke-width="1" stroke-dasharray="4 6"/>
      <path d="${area}" fill="url(#fill-${options.id || 'a'})"/>
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"
        stroke-linecap="round" vector-effect="non-scaling-stroke" class="draw"/>
      <circle class="cursor" r="4" fill="${color}" stroke="#0a0d12" stroke-width="2" opacity="0"/>
    `;
    const cursor = svg.querySelector('.cursor');
    let pinned = false;
    const average = values.reduce((sum, v) => sum + v, 0) / Math.max(values.length, 1);
    const show = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      const index = Math.round(ratio * (series.length - 1));
      const point = series[index];
      cursor.setAttribute('cx', x(index));
      cursor.setAttribute('cy', y(values[index]));
      cursor.setAttribute('opacity', '1');
      if (tip) {
        tip.hidden = false;
        const delta = average > 0
          ? Math.round(((Number(point.value) || 0) - average) / average * 100) : 0;
        tip.textContent = `${dateShort(point.date)} · ${money(point.value, { digits: 0 })} · ${delta > 0 ? '+' : ''}${delta}% к среднему`;
        const left = (x(index) / width) * rect.width;
        tip.style.left = `${Math.min(Math.max(left, 40), rect.width - 40)}px`;
      }
    };
    svg.addEventListener('pointermove', (event) => show(event.clientX));
    svg.addEventListener('pointerdown', (event) => show(event.clientX));
    svg.addEventListener('pointerleave', () => {
      if (pinned) return;
      cursor.setAttribute('opacity', '0');
      if (tip) tip.hidden = true;
    });
    svg.addEventListener('pointerdown', (event) => {
      pinned = !pinned;
      if (pinned) show(event.clientX);
      else {
        cursor.setAttribute('opacity', '0');
        if (tip) tip.hidden = true;
      }
    });
    host.append(svg);
    const middle = series[Math.floor(series.length / 2)];
    host.append(el('div', { class: 'chart-axis' },
      el('span', {}, dateShort(series[0].date)),
      el('span', {}, middle ? dateShort(middle.date) : ''),
      el('span', {}, dateShort(series[series.length - 1].date))));
    if (document.documentElement.dataset.motion !== 'off') {
      const drawn = svg.querySelector('path.draw');
      try {
        const length = drawn.getTotalLength();
        drawn.style.strokeDasharray = String(length);
        drawn.style.strokeDashoffset = String(length);
        requestAnimationFrame(() => {
          drawn.style.transition = 'stroke-dashoffset 620ms cubic-bezier(.22,.61,.36,1)';
          drawn.style.strokeDashoffset = '0';
        });
      } catch { /* geometry unavailable: show the final path */ }
    }
  }

  async function loadOverview(days) {
    state.chartDays = days;
    try {
      state.data.overview = await api(`/api/cfo/overview?days=${days}`);
      renderDashboard();
    } catch (error) { toast(error.message, 'bad'); }
  }

  function donutChart(slices) {
    const size = 132, radius = 52, stroke = 14, center = size / 2;
    const total = slices.reduce((sum, s) => sum + Math.abs(Number(s.value) || 0), 0);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
    const colors = ['#3ddc97', '#6ba6ff', '#f5b45c', '#a78bfa', '#f2707a', '#4dd0e1', '#8b95a7'];
    let offset = 0;
    const circumference = 2 * Math.PI * radius;
    if (total <= 0) {
      svg.innerHTML = `<circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#1a2029" stroke-width="${stroke}"/>
        <text x="${center}" y="${center + 4}" text-anchor="middle">нет трат</text>`;
      return svg;
    }
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', center); label.setAttribute('y', center + 4);
    label.setAttribute('text-anchor', 'middle');
    label.textContent = compact(total) + ' ₽';
    const circles = [];
    slices.slice(0, 6).forEach((slice, index) => {
      const share = Math.abs(Number(slice.value)) / total;
      const dash = share * circumference;
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', center); circle.setAttribute('cy', center); circle.setAttribute('r', radius);
      circle.setAttribute('fill', 'none');
      circle.setAttribute('stroke', colors[index % colors.length]);
      circle.setAttribute('stroke-width', stroke);
      circle.setAttribute('stroke-dasharray', `${Math.max(dash - 3, 0)} ${circumference - Math.max(dash - 3, 0)}`);
      circle.setAttribute('stroke-dashoffset', -offset);
      circle.setAttribute('transform', `rotate(-90 ${center} ${center})`);
      circle.setAttribute('stroke-linecap', 'round');
      circle.style.cursor = 'pointer';
      circle.addEventListener('pointerenter', () => {
        circles.forEach((item) => item.setAttribute('opacity', item === circle ? '1' : '.35'));
        label.textContent = `${titleFor(slice.label)} · ${money(slice.value, { digits: 0 })}`;
      });
      circle.addEventListener('pointerleave', () => {
        circles.forEach((item) => item.setAttribute('opacity', '1'));
        label.textContent = compact(total) + ' ₽';
      });
      svg.append(circle);
      circles.push(circle);
      offset += dash;
    });
    svg.append(label);
    return svg;
  }

  /* --------------------------- cinematic bg ------------------------------ */
  const bg = { gl: null, program: null, buffer: null, raf: 0, mode: 'waves', dpr: 1,
    pointer: { x: 0.5, y: 0.5, vx: 0, vy: 0 }, time: 0, lost: false, canvas: null };

  const BG_VERT = 'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}';
  const BG_FRAG = `
    precision mediump float;
    uniform vec2 u_res; uniform float u_time; uniform vec2 u_mouse; uniform float u_mode; uniform float u_vel;
    float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
    float noise(vec2 p){
      vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
      return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),f.x),f.y);
    }
    void main(){
      vec2 uv=gl_FragCoord.xy/u_res;
      vec2 p=(gl_FragCoord.xy-0.5*u_res)/u_res.y;
      float t=u_time;
      float wave=0.0;
      if(u_mode<0.5){
        for(int i=0;i<4;i++){
          float fi=float(i);
          float y=p.y+sin(p.x*(2.2+fi*0.7)+t*(0.25+fi*0.05)+fi)*0.16;
          wave+=smoothstep(0.55,0.0,abs(y+0.15+fi*0.12))*(0.22-fi*0.04);
        }
      } else {
        float d=distance(p,u_mouse*vec2(1.6,1.0)-vec2(0.8,0.5));
        float ripple=sin(d*14.0-t*1.6)*exp(-d*2.6);
        float warp=noise(p*2.4+t*0.06)*0.6+noise(p*5.0-t*0.04)*0.4;
        wave=(ripple*0.55+warp*0.35)*(0.55+u_vel);
      }
      vec3 deep=vec3(0.031,0.043,0.062);
      vec3 mint=vec3(0.16,0.72,0.52);
      vec3 ice=vec3(0.32,0.55,0.92);
      vec3 col=mix(deep,deep*1.25,uv.y);
      col+=mint*wave*0.30;
      col+=ice*smoothstep(0.7,0.0,abs(p.y+0.35+sin(p.x*1.4-t*0.2)*0.08))*0.10;
      float glow=smoothstep(0.9,0.0,distance(uv,vec2(0.2,0.9)))*0.10;
      col+=mint*glow;
      gl_FragColor=vec4(col,1.0);
    }`;

  function bgCompile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  function bgSetup() {
    const canvas = bg.canvas;
    if (!canvas || bg.lost) return false;
    const gl = canvas.getContext('webgl', { antialias: false, alpha: false,
      powerPreference: 'low-power', preserveDrawingBuffer: false });
    if (!gl) return false;
    const vert = bgCompile(gl, gl.VERTEX_SHADER, BG_VERT);
    const frag = bgCompile(gl, gl.FRAGMENT_SHADER, BG_FRAG);
    if (!vert || !frag) return false;
    const program = gl.createProgram();
    gl.attachShader(program, vert);
    gl.attachShader(program, frag);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return false;
    bg.gl = gl;
    bg.program = program;
    bg.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, bg.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.useProgram(program);
    const attr = gl.getAttribLocation(program, 'a');
    gl.enableVertexAttribArray(attr);
    gl.vertexAttribPointer(attr, 2, gl.FLOAT, false, 0, 0);
    bg.uniforms = {
      res: gl.getUniformLocation(program, 'u_res'),
      time: gl.getUniformLocation(program, 'u_time'),
      mouse: gl.getUniformLocation(program, 'u_mouse'),
      mode: gl.getUniformLocation(program, 'u_mode'),
      vel: gl.getUniformLocation(program, 'u_vel'),
    };
    bgResize();
    return true;
  }

  function bgResize() {
    const canvas = bg.canvas;
    if (!canvas || !bg.gl) return;
    const mobile = window.matchMedia('(max-width: 1024px)').matches;
    bg.dpr = Math.min(window.devicePixelRatio || 1, mobile ? 1.25 : 1.5);
    const width = Math.max(1, Math.floor(window.innerWidth * bg.dpr));
    const height = Math.max(1, Math.floor(window.innerHeight * bg.dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      bg.gl.viewport(0, 0, width, height);
    }
  }

  function bgFrame(now) {
    const gl = bg.gl;
    if (!gl || !bg.program) return;
    const dt = Math.min(0.05, (now - (bg.last || now)) / 1000);
    bg.last = now;
    if (bg.mode !== 'static') bg.time += dt;
    const { res, time, mouse, mode, vel } = bg.uniforms;
    gl.uniform2f(res, bg.canvas.width, bg.canvas.height);
    gl.uniform1f(time, bg.time);
    gl.uniform2f(mouse, bg.pointer.x, bg.pointer.y);
    gl.uniform1f(mode, bg.mode === 'fluid' ? 1 : 0);
    gl.uniform1f(vel, Math.min(1, Math.abs(bg.pointer.vx) + Math.abs(bg.pointer.vy)));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    bg.pointer.vx *= 0.92;
    bg.pointer.vy *= 0.92;
    if (bg.mode === 'static') { bg.raf = 0; return; }
    bg.raf = requestAnimationFrame(bgFrame);
  }

  function bgStart() {
    if (!bg.gl || document.hidden || document.documentElement.dataset.motion === 'off') return;
    if (bg.mode === 'static' || bg.raf) return;
    bg.last = 0;
    bg.raf = requestAnimationFrame(bgFrame);
  }

  function bgStop() {
    if (bg.raf) cancelAnimationFrame(bg.raf);
    bg.raf = 0;
  }

  function setBackgroundMode(mode, { persist = true } = {}) {
    const value = ['waves', 'fluid', 'static'].includes(mode) ? mode : 'waves';
    bg.mode = value;
    document.documentElement.dataset.bg = value;
    if (persist) localStorage.setItem('jarvis.background', value);
    $$('#bg-mode button').forEach((button) => button.classList.toggle('on', button.dataset.bg === value));
    bgStop();
    if (value === 'static') {
      if (bg.gl) { bg.time = 12; bgFrame(performance.now()); }
      return;
    }
    bgStart();
  }

  function initBackground() {
    const canvas = $('#bg-canvas');
    if (!canvas) return;
    bg.canvas = canvas;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!bgSetup()) { canvas.hidden = true; return; }
    const saved = localStorage.getItem('jarvis.background');
    const initial = saved || (reduced ? 'static' : 'waves');
    setBackgroundMode(initial, { persist: false });
    window.addEventListener('resize', () => { bgResize(); if (bg.mode === 'static') bgFrame(performance.now()); });
    window.addEventListener('pointermove', (event) => {
      const x = event.clientX / window.innerWidth;
      const y = 1 - event.clientY / window.innerHeight;
      bg.pointer.vx += Math.abs(x - bg.pointer.x) * 6;
      bg.pointer.vy += Math.abs(y - bg.pointer.y) * 6;
      bg.pointer.x = x;
      bg.pointer.y = y;
    }, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) bgStop(); else if (bg.mode !== 'static') bgStart();
    });
    canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      bg.lost = true;
      bgStop();
      canvas.hidden = true;
    });
    canvas.addEventListener('webglcontextrestored', () => {
      bg.lost = false;
      if (bgSetup()) { canvas.hidden = false; setBackgroundMode(bg.mode, { persist: false }); }
    });
  }

  /* --------------------------- number motion ----------------------------- */
  const numericState = new WeakMap();
  function countTo(node, value, formatter) {
    if (!node) return;
    const target = Number(value);
    if (!Number.isFinite(target)) { node.textContent = formatter(null); return; }
    const motionOff = document.documentElement.dataset.motion === 'off';
    const previous = numericState.get(node);
    numericState.set(node, target);
    if (motionOff || previous === undefined || previous === target) {
      node.textContent = formatter(target);
      return;
    }
    const started = performance.now();
    const step = (now) => {
      const progress = Math.min(1, (now - started) / 420);
      const eased = 1 - Math.pow(1 - progress, 3);
      node.textContent = formatter(previous + (target - previous) * eased);
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  /* --------------------------- viewport height --------------------------- */
  function bindViewportHeight() {
    const apply = () => {
      const viewport = window.visualViewport;
      const height = viewport ? viewport.height : window.innerHeight;
      document.documentElement.style.setProperty('--app-h', `${Math.round(height)}px`);
    };
    apply();
    window.visualViewport?.addEventListener('resize', apply);
    window.addEventListener('orientationchange', () => setTimeout(apply, 120));
  }

  /* -------------------------------- sheet -------------------------------- */
  let sheetReturnFocus = null;
  function sheetFocusables() {
    const panel = $('#sheet .sheet-panel');
    if (!panel) return [];
    return $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])', panel)
      .filter((node) => node.offsetParent !== null);
  }
  function trapSheetFocus(event) {
    if (event.key !== 'Tab') return;
    const items = sheetFocusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openSheet(title, body, actions = []) {
    const sheet = $('#sheet');
    sheetReturnFocus = document.activeElement;
    $('#sheet-title').textContent = title;
    const host = $('#sheet-body');
    host.replaceChildren();
    if (body) host.append(body);
    if (actions.length) {
      const row = el('div', { class: 'sheet-actions' });
      actions.forEach((action) => row.append(el('button', {
        type: 'button', class: `btn ${action.kind || ''}`.trim(),
        onclick: () => action.run(closeSheet),
      }, action.label)));
      host.append(row);
    }
    sheet.hidden = false;
    sheet.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', trapSheetFocus);
    const first = host.querySelector('input, select, button');
    if (first) setTimeout(() => first.focus(), 60);
  }
  function closeSheet() {
    const sheet = $('#sheet');
    sheet.hidden = true;
    sheet.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    document.removeEventListener('keydown', trapSheetFocus);
    if (sheetReturnFocus && typeof sheetReturnFocus.focus === 'function') {
      try { sheetReturnFocus.focus(); } catch { /* element may be gone */ }
    }
    sheetReturnFocus = null;
  }

  function field(label, input) {
    return el('label', { class: 'field' }, el('span', {}, label), input);
  }
  function input(attrs) { return el('input', attrs); }

  /* ------------------------------ data load ------------------------------ */
  async function loadCore() {
    const results = await Promise.allSettled([
      api('/api/cfo'), api('/api/cfo/overview'), api('/api/cfo/briefing'),
      api('/api/cfo/analytics'), api('/api/cfo/goals'), api('/api/cfo/runtime'),
      api('/api/cfo/categories'), api('/api/cfo/proposal-groups'),
    ]);
    const keys = ['cfo', 'overview', 'briefing', 'analytics', 'goals', 'runtime', 'categories', 'groups'];
    const failed = [];
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') state.data[keys[index]] = result.value;
      else failed.push(keys[index]);
    });
    if (state.data.categories) state.catalog = state.data.categories;
    state.loaded.core = true;
    if (failed.includes('cfo')) throw new ApiError('Не удалось прочитать локальную базу', 500);
    return failed;
  }

  async function loadHistory(reset = false) {
    if (reset) {
      state.history = { items: [], offset: 0, total: 0, categories: [], sources: [] };
    }
    const today = new Date();
    const start = new Date(today.getTime() - state.filters.days * 86400000);
    const iso = (d) => d.toISOString().slice(0, 10);
    const payload = {
      start: iso(start), end: iso(today), kind: state.filters.kind,
      confirmation: state.filters.confirmation, category: state.filters.category,
      q: state.filters.q, offset: state.history.offset, limit: 25,
    };
    const data = await api('/api/cfo/history', { method: 'POST', body: payload });
    state.history.items = reset ? data.items : state.history.items.concat(data.items);
    state.history.offset = data.next_offset ?? state.history.items.length;
    state.history.total = data.total;
    state.history.next = data.next_offset;
    state.history.categories = data.categories || [];
    state.history.sources = data.sources || [];
    state.history.notice = data.notice;
    return data;
  }

  async function loadPendingCount() {
    try {
      const data = await api('/api/cfo/history', {
        method: 'POST',
        body: {
          kind: 'unknown', limit: 1,
          start: new Date(Date.now() - 365 * 86400000).toISOString().slice(0, 10),
        },
      });
      return data.total;
    } catch { return null; }
  }
  /* ------------------------------ dashboard ------------------------------ */
  function renderDashboard() {
    const cfo = state.data.cfo || {};
    const overview = state.data.overview || {};
    const position = cfo.financial_position || {};
    const profile = cfo.profile || {};
    const bank = cfo.bank || null;

    // Hero: total known money
    const totalLow = position.total_low;
    const totalHigh = position.total_high;
    const same = totalLow !== null && totalHigh !== null && Number(totalLow) === Number(totalHigh);
    $('#hero-total').textContent = totalLow === null || totalLow === undefined
      ? 'Нет данных'
      : same ? money(totalLow) : `${money(totalLow)} – ${money(totalHigh)}`;
    const fresh = Boolean(cfo.bank_fresh);
    const observed = bank?.observed_at;
    const freshness = $('#hero-freshness');
    freshness.className = `chip ${fresh ? 'ok' : 'warn'}`;
    freshness.replaceChildren(el('i', { class: 'dot' }), fresh ? 'данные свежие' : `снимок ${ago(observed)}`);
    $('#hero-sub').textContent = bank
      ? `Банк прочитан ${dateFull(observed)} · история ${coverageLabel(cfo.history_coverage)}`
      : 'Банковский снимок отсутствует — показаны только ручные данные';

    const split = $('#hero-split');
    split.replaceChildren(
      el('div', {}, el('span', {}, 'Банк, Сбер'), el('b', {}, money(position.bank_total ?? cfo.bank_rub))),
      el('div', {}, el('span', {}, 'Наличные (оценка)'), el('b', {}, profile.cash ? money(profile.cash.low) + (profile.cash.high && profile.cash.high !== profile.cash.low ? '–' + money(profile.cash.high) : '') : 'не заданы')),
      el('div', {}, el('span', {}, 'Резерв'), el('b', {}, money(position.reserve_effective ?? '0'))),
      el('div', {}, el('span', {}, 'Под цели'), el('b', {}, money(position.goal_reserved ?? '0'))),
    );

    const warning = $('#hero-warning');
    const missing = position.missing || [];
    if (missing.length) {
      warning.hidden = false;
      warning.replaceChildren(
        el('span', {}, 'Данные неполные'),
        el('details', {}, el('summary', {}, 'Что уточнить'),
          el('span', {}, missing.slice(0, 4).join(' · '))));
    } else {
      warning.hidden = true;
    }

    // Safe to spend
    const until = position.until_next_income || {};
    const base = until.base || {};
    const headroom = base.headroom;
    const negative = headroom !== null && headroom !== undefined && Number(headroom) < 0;
    countTo($('#safe-amount'), negative ? Math.abs(Number(headroom)) : headroom, (value) =>
      headroom === null || headroom === undefined ? 'Недостаточно данных'
        : negative ? `Дефицит ${money(value)}` : money(value));
    styled($('#safe-amount'), negative ? { color: 'var(--warn)' } : { color: '' });
    const horizon = position.horizon_days;
    $('#safe-sub').textContent = position.next_income
      ? `До ${dateShort(position.next_income)} · ${horizon ?? '?'} дн. · базовый сценарий${negative ? ' не покрывает расходы' : ''}`
      : 'Дата следующего дохода не задана';
    const scenarios = $('#safe-scenarios');
    scenarios.replaceChildren();
    [['Пессимистичный', until.pessimistic], ['Базовый', until.base], ['Оптимистичный', until.optimistic]]
      .forEach(([label, scenario]) => {
        if (!scenario) return;
        scenarios.append(el('div', {}, el('span', {}, label), el('b', { class: Number(scenario.headroom) < 0 ? 'down' : '' }, money(scenario.headroom))));
      });
    $('#safe-fineprint').textContent = position.obligations_complete
      ? 'Обязательства отмечены полностью; сценарии всё равно остаются оценкой.'
      : 'Список обязательств может быть неполным — задайте его в настройках для точного прогноза.';

    // One contextual action: the most useful next step for the current state.
    const actionHost = $('#safe-action');
    actionHost.replaceChildren();
    const pendingUnknown = Number(state.data.pendingUnknown || 0);
    if (negative) {
      actionHost.append(el('button', {
        class: 'btn small', type: 'button', onclick: () => { location.hash = '#/plan'; },
      }, 'Открыть планирование'));
    } else if (pendingUnknown > 0) {
      actionHost.append(el('button', {
        class: 'btn small', type: 'button',
        onclick: () => { location.hash = '#/operations'; state.filters.kind = 'unknown'; reloadOperations(); },
      }, `Разобрать ${pendingUnknown} ${plural(pendingUnknown, 'операцию', 'операции', 'операций')}`));
    } else {
      actionHost.append(el('button', {
        class: 'btn small', type: 'button', onclick: () => openQuickAdd('expense'),
      }, 'Записать расход'));
    }

    // Month metrics
    const flow = overview.flow || cfo.cashflow || {};
    countTo($('#m-spent'), flow.expenses, (value) => money(value));
    const delta = state.data.analytics?.week_expense_delta;
    $('#m-spent-note').textContent = delta && Number(delta) !== 0
      ? `к прошлой неделе ${Number(delta) > 0 ? '+' : ''}${money(delta)}`
      : 'без своих переводов';
    countTo($('#m-income'), flow.income, (value) => money(value));
    $('#m-income-note').textContent = 'только зачисленное';
    countTo($('#m-bank'), cfo.bank_rub, (value) => money(value));
    $('#m-bank-note').textContent = bank
      ? `${plural(bank.accounts?.length || 0, 'счёт', 'счёта', 'счетов')} · ${bank.held_operations || 0} на проверке`
      : 'Сбер не подключён';

    // 30-day chart
    const daily = overview.daily || [];
    const chart = $('#month-chart');
    spendChart(chart, daily.map((d) => ({ date: d.date, value: d.expenses })), { id: 'exp' });
    const total30 = daily.reduce((sum, d) => sum + Number(d.expenses || 0), 0);
    $$('#chart-period button').forEach((button) =>
      button.classList.toggle('on', Number(button.dataset.days) === state.chartDays));
    $('#month-legend').replaceChildren(
      el('span', {}, styled(el('i'), { background: '#3ddc97' }), 'Расходы по дням'),
      el('span', {}, `${money(total30, { digits: 0 })} за ${state.chartDays} дн.`),
      el('span', {}, `Средний день: ${money(cfo.daily_spend_baseline)}`),
      el('span', {}, `Макс. день: ${money(Math.max(...daily.map((d) => Number(d.expenses) || 0)), { digits: 0 })}`),
    );

    // Categories donut
    const categories = (overview.categories || []).filter((c) => Number(c.spent) > 0);
    const donut = $('#cat-donut');
    donut.replaceChildren(donutChart(categories.map((c) => ({ value: c.spent, label: c.category }))));
    const list = $('#cat-list');
    list.replaceChildren();
    const catTotal = categories.reduce((sum, c) => sum + Number(c.spent), 0);
    categories.slice(0, 5).forEach((cat) => {
      const share = catTotal ? Math.round((Number(cat.spent) / catTotal) * 100) : 0;
      list.append(el('li', {},
        el('span', { class: 'cat-name' }, titleFor(cat.category)),
        el('span', { class: 'cat-share' }, `${share}%`),
        el('b', {}, money(cat.spent, { digits: 0 }))));
    });
    if (!categories.length) list.append(el('li', {}, 'Трат за месяц пока нет'));

    // Recent operations
    const mobile = window.matchMedia('(max-width: 560px)').matches;
    const recent = (cfo.transactions || []).slice(0, mobile ? 4 : 6);
    const ops = $('#recent-ops');
    ops.replaceChildren();
    if (!recent.length) ops.append(el('li', {}, el('p', { class: 'empty' }, 'Операций пока нет')));
    recent.forEach((row, index) => {
      const node = opRow(row, false);
      styled(node, { '--i': String(index) });
      ops.append(node);
    });

    // Insights from briefing
    // Two compact Coin lines instead of a separate card.
    const events = (state.data.briefing?.events || []).slice(0, 2);
    const insightLine = $('#chart-insight');
    const coinLine = $('#coin-note-line');
    if (events[0]) {
      insightLine.hidden = false;
      const reason = String(events[0].reason || '').replace(/\s+/g, ' ').slice(0, 110);
      insightLine.textContent = `Coin: ${events[0].title || 'наблюдение'}${reason ? ' — ' + reason : ''}`;
    } else {
      insightLine.hidden = true;
    }
    if (coinLine) {
      const second = events[1];
      coinLine.textContent = second
        ? `Coin: ${second.title || 'наблюдение'}`
        : 'Coin подскажет, где сэкономить — откройте раздел Coin.';
    }

    // Goals
    renderGoalStrip($('#dash-goals'), (state.data.goals?.items || []).slice(0, 3), 'Нет целей. Добавьте первую в планировании.');
  }

  function opRow(row, withActions) {
    const amount = Number(row.amount);
    const isIncome = amount > 0;
    const iconName = row.movement_kind === 'transfer' ? 'i-refresh' : isIncome ? 'i-arrow-down' : 'i-arrow-up';
    const button = el('button', { type: 'button', class: 'op-open', onclick: () => openOperation(row) },
      el('div', { class: 'op-main' },
        el('span', { class: 'op-icon' }, icon(iconName)),
        el('div', { class: 'op-text' },
          el('b', {}, row.merchant || row.description || 'Операция'),
          el('span', {}, `${dateShort(row.tx_date)} · ${titleFor(row.category)}${row.confirmed ? ' · подтверждено' : ''}`))),
      el('div', { class: 'op-amount' },
        el('span', { class: signed(amount) }, money(Math.abs(amount))),
        el('small', {}, row.movement_kind === 'transfer' ? 'перевод' : isIncome ? 'поступление' : 'расход')));
    const li = el('li', {}, button);
    if (withActions && !row.confirmed) {
      const actions = el('div', { class: 'op-actions' });
      if (row.category && !['unknown', 'uncategorized', 'pending_review'].includes(row.category)) {
        actions.append(el('button', {
          class: 'btn ghost small', type: 'button',
          onclick: (event) => { event.stopPropagation(); confirmCategory(row); },
        }, icon('i-check'), 'Подтвердить'));
      }
      actions.append(el('button', {
        class: 'btn ghost small', type: 'button',
        onclick: (event) => { event.stopPropagation(); openCategoryPicker(row); },
      }, icon('i-edit'), 'Изменить'));
      li.append(actions);
    }
    return li;
  }

  function renderGoalStrip(host, goals, emptyText) {
    host.replaceChildren();
    if (!goals.length) {
      host.append(el('div', { class: 'goal-empty' },
        el('div', {},
          el('b', {}, 'Целей пока нет'),
          el('span', {}, emptyText)),
        el('button', {
          class: 'btn small', type: 'button',
          onclick: () => { location.hash = '#/plan'; openGoal(null); },
        }, 'Создать цель')));
      return;
    }
    goals.forEach((goal) => {
      const progress = pct(goal.current_amount, goal.target_amount);
      host.append(el('div', { class: 'goal' },
        el('div', { class: 'goal-top' }, el('b', {}, goal.name), el('span', {}, `${Math.round(progress)}%`)),
        el('div', { class: 'goal-bar' }, styled(el('i'), { width: progress + '%' })),
        el('small', {}, `${money(goal.current_amount, { digits: 0 })} из ${money(goal.target_amount, { digits: 0 })}${goal.deadline ? ' · до ' + dateShort(goal.deadline) : ''}`)));
    });
  }

  /* ----------------------------- operations ------------------------------ */
  function renderQueue() {
    const panel = $('#queue-panel');
    if (!panel) return;
    const groups = (state.data.groups?.groups || []).filter((group) =>
      (group.items || []).some((item) => item.needs_review));
    if (!groups.length) { panel.hidden = true; return; }
    panel.hidden = false;
    $('#queue-title').textContent = `${plural(groups.length, 'продавец ждёт', 'продавца ждут', 'продавцов ждут')} решения`;
    $('#queue-sub').textContent = state.data.groups?.notice
      || 'Подтвердите категорию сразу для похожих операций — это правило запомнится локально.';
    const list = $('#queue-list');
    list.replaceChildren();
    groups.slice(0, 8).forEach((group) => {
      const pending = (group.items || []).filter((item) => item.needs_review);
      const suggested = pending.find((item) => item.proposed_category && item.proposed_category !== 'unknown')?.proposed_category;
      list.append(el('li', {},
        el('div', { class: 'queue-name' },
          el('b', {}, group.merchant || 'Без названия'),
          el('span', {}, `${pending.length} из ${group.items?.length || pending.length} требуют решения`)),
        el('span', { class: 'queue-hint' }, suggested ? `Coin: ${titleFor(suggested)}` : (group.reasons || []).slice(0, 2).map(reasonLabel).join(' · ')),
        el('button', {
          class: 'btn small', type: 'button',
          onclick: () => openGroupReview(group),
        }, 'Разобрать')));
    });
  }

  function openGroupReview(group) {
    const pending = (group.items || []).filter((item) => item.needs_review);
    let category = pending.find((item) => item.proposed_category && item.proposed_category !== 'unknown')?.proposed_category || '';
    const wrap = el('div', { class: 'cat-picker' });
    const income = Number((pending[0] || group.items?.[0] || {}).amount) > 0;
    const groups = pickerGroups(income);
    Object.entries(groups).forEach(([bucket, items]) => {
      const grid = el('div', { class: 'cat-grid' });
      items.forEach((item) => {
        const button = el('button', {
          type: 'button', class: item.category === category ? 'on' : '',
          onclick: () => {
            category = item.category;
            $$('.cat-grid button', wrap).forEach((b) => b.classList.remove('on'));
            button.classList.add('on');
          },
        }, item.title);
        grid.append(button);
      });
      wrap.append(el('div', { class: 'cat-group' }, el('h4', {}, bucket), grid));
    });
    const body = el('div', { class: 'stack' },
      el('p', { class: 'body-text' }, `${group.merchant || 'Продавец'} · ${plural(pending.length, 'операция', 'операции', 'операций')}. Категория применится ко всей группе одной записью.`),
      wrap);
    openSheet('Разобрать группу', body, [
      { label: 'Применить ко всей группе', kind: 'primary', run: async (close) => {
        if (!category) { toast('Выберите категорию', 'bad'); return; }
        try {
          const result = await api('/api/cfo/group-review', { method: 'POST', body: {
            group_id: group.id,
            items: pending.map((item) => ({
              id: item.id, expected_version: item.expected_version, input_sha256: item.input_sha256,
            })),
            category,
          } });
          toast(`Сохранено: ${result.count} операций`, 'ok');
          close();
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  function renderOperations(reset = true) {
    renderQueue();
    const list = $('#op-list');
    if (reset) list.replaceChildren();
    const items = state.history.items;
    $('#op-summary').textContent = `Найдено ${state.history.total} операций · показано ${items.length}`;
    $('#op-empty').hidden = items.length > 0;
    $('#op-more').hidden = !state.history.next;
    if (reset) {
      items.forEach((row, index) => {
        const node = opRow(row, true);
        styled(node, { '--i': String(Math.min(index, 14)) });
        list.append(node);
      });
      const select = $('#op-category');
      const current = select.value;
      select.replaceChildren(el('option', { value: '' }, 'Все категории'),
        ...state.history.categories.map((cat) => el('option', { value: cat }, titleFor(cat))));
      select.value = current;
    } else {
      items.slice(list.children.length).forEach((row, index) => {
        const node = opRow(row, true);
        styled(node, { '--i': String(Math.min(index, 8)) });
        list.append(node);
      });
    }
  }

  function openOperation(row) {
    const amount = Number(row.amount);
    const body = el('div', { class: 'stack' },
      el('p', { class: 'hero-value sm' }, money(Math.abs(amount))),
      el('dl', { class: 'kv' },
        el('div', {}, el('dt', {}, 'Дата'), el('dd', {}, dateFull(row.tx_date))),
        el('div', {}, el('dt', {}, 'Категория'), el('dd', {}, titleFor(row.category))),
        el('div', {}, el('dt', {}, 'Статус'), el('dd', {}, row.confirmed ? 'Подтверждено вами' : 'Не подтверждено')),
        el('div', {}, el('dt', {}, 'Счёт'), el('dd', {}, row.account || '—')),
        el('div', {}, el('dt', {}, 'Источник'), el('dd', {}, (row.sources || []).join(', ') || row.account || '—'))),
      el('p', { class: 'body-text' }, row.classification_reason || ''),
      el('p', { class: 'body-text' }, row.description || ''));
    const actions = [
      { label: 'Изменить категорию', kind: 'primary', run: () => openCategoryPicker(row) },
    ];
    if (row.confirmed) actions.push({ label: 'Отменить подтверждение', kind: 'ghost', run: () => undoCategory(row) });
    openSheet('Операция', body, actions);
  }

  function openCategoryPicker(row) {
    const body = el('div', { class: 'cat-picker' });
    const merged = pickerGroups(Number(row.amount) >= 0);
    const chosen = { value: row.category };
    const wrap = el('div', { class: 'cat-picker' });
    Object.entries(merged).forEach(([bucket, items]) => {
      const grid = el('div', { class: 'cat-grid' });
      items.forEach((item) => {
        const button = el('button', {
          type: 'button', class: item.category === row.category ? 'on' : '',
          onclick: () => {
            chosen.value = item.category;
            $$('.cat-grid button', wrap).forEach((b) => b.classList.remove('on'));
            button.classList.add('on');
          },
        }, item.title);
        grid.append(button);
      });
      wrap.append(el('div', { class: 'cat-group' }, el('h4', {}, bucket), grid));
    });
    body.append(wrap);
    openSheet('Категория операции', body, [
      { label: 'Сохранить', kind: 'primary', run: async (close) => {
        if (!chosen.value) return;
        await saveCategory(row, chosen.value);
        close();
      } },
    ]);
  }

  async function saveCategory(row, category) {
    try {
      const result = await api(`/api/cfo/category/${row.id}`, {
        method: 'PUT',
        body: { category, expected_version: row.correction_version ?? 0 },
      });
      if (result.status === 'saved') {
        toast('Категория сохранена', 'ok');
        await refreshAfterEdit();
      } else {
        toast('Операция изменилась, обновите список', 'bad');
      }
    } catch (error) { toast(error.message, 'bad'); }
  }

  async function confirmCategory(row) {
    await saveCategory(row, row.category);
  }

  async function undoCategory(row) {
    try {
      const result = await api(`/api/cfo/category/${row.id}/undo`, {
        method: 'POST', body: { expected_version: row.correction_version ?? 0 },
      });
      if (result.status === 'saved') { toast('Подтверждение отменено', 'ok'); await refreshAfterEdit(); }
      else toast('Нечего отменять', 'bad');
    } catch (error) { toast(error.message, 'bad'); }
  }

  async function refreshAfterEdit() {
    await loadCore();
    if (state.loaded.history) { await loadHistory(true); renderOperations(true); }
    renderCurrentPage();
    renderPendingBadges();
  }

  async function renderPendingBadges() {
    const count = await loadPendingCount();
    state.data.pendingUnknown = count ?? 0;
    [$('#nav-pending'), $('#nav-pending-m')].forEach((badge) => {
      if (!badge) return;
      if (count) { badge.textContent = String(count); badge.hidden = false; }
      else badge.hidden = true;
    });
    const host = $('#pending-count');
    if (host) host.textContent = count === null ? '—' : String(count);
    if (currentRoute() === 'dashboard' && state.loaded.core) renderDashboard();
  }

  /* ------------------------------- budget -------------------------------- */
  function renderBudget() {
    const overview = state.data.overview || {};
    const categories = (overview.categories || []).filter((c) => Number(c.spent) > 0 || c.limit);
    const limits = overview.limits || {};
    const month = overview.month || '';
    const monthName = month ? new Intl.DateTimeFormat('ru-RU', { month: 'long', year: 'numeric' }).format(new Date(month + '-01T00:00:00')) : '';
    $('#budget-title').textContent = monthName ? monthName[0].toUpperCase() + monthName.slice(1) : 'Текущий месяц';
    const spent = Number(overview.flow?.expenses || 0);
    const limitSum = Object.values(limits).reduce((sum, value) => sum + Number(value || 0), 0);
    $('#budget-total-chip').textContent = limitSum ? `${money(spent, { digits: 0 })} из ${money(limitSum, { digits: 0 })}` : `${money(spent, { digits: 0 })} без лимитов`;
    $('#budget-total-chip').className = `chip ${limitSum && spent > limitSum ? 'bad' : 'quiet'}`;
    const bar = $('#budget-bar');
    bar.replaceChildren();
    if (limitSum) {
      bar.append(styled(el('i'), {
        width: pct(spent, limitSum) + '%',
        background: spent > limitSum ? 'var(--danger)' : 'var(--accent)',
      }));
    }

    const list = $('#budget-list');
    list.replaceChildren();
    const all = Object.entries(limits).map(([category, limit]) => ({
      category, limit: Number(limit),
      spent: Number((categories.find((c) => c.category === category) || {}).spent || 0),
    }));
    categories.forEach((cat) => {
      if (!(cat.category in limits)) all.push({ category: cat.category, limit: null, spent: Number(cat.spent) });
    });
    if (!all.length) list.append(el('li', {}, el('p', { class: 'empty' }, 'Пока нет трат для бюджета')));
    all.sort((a, b) => b.spent - a.spent);
    all.forEach((row) => {
      const limitInput = el('input', {
        inputmode: 'decimal', placeholder: 'задать', value: row.limit ?? '',
        'aria-label': `Лимит для ${titleFor(row.category)}`,
      });
      const save = el('button', { class: 'budget-save', type: 'button', title: 'Сохранить лимит', 'aria-label': 'Сохранить лимит' },
        icon('i-check'));
      save.addEventListener('click', async () => {
        const raw = limitInput.value.trim();
        if (raw === '') { toast('Введите сумму лимита', 'bad'); return; }
        try {
          await api('/api/cfo/category-budget', { method: 'PUT', body: { category: row.category, monthly_limit: raw.replace(',', '.') } });
          toast('Лимит сохранён', 'ok');
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      });
      const share = row.limit ? pct(row.spent, row.limit) : 0;
      const nameCell = el('div', { class: 'budget-name' }, el('b', {}, titleFor(row.category)));
      if (row.limit) {
        nameCell.append(el('div', { class: 'bar' },
          styled(el('i', { class: share >= 100 ? 'over' : '' }), { width: share + '%' })));
      }
      list.append(el('li', {},
        nameCell,
        el('span', { class: 'budget-spent' }, money(row.spent, { digits: 0 })),
        el('div', { class: 'budget-limit' }, limitInput),
        save));
    });

    const recurring = $('#recurring-list');
    recurring.replaceChildren();
    const patterns = state.data.cfo?.patterns || [];
    const repeated = patterns.filter((p) => p.kind === 'expense_candidate').slice(0, 6);
    const unusual = patterns.filter((p) => p.kind === 'unusual_expense_candidate').slice(0, 4);
    if (repeated.length) {
      repeated.forEach((pattern) => recurring.append(el('li', {},
        el('span', {}, pattern.merchant || titleFor(pattern.category)),
        el('b', {}, money(pattern.typical_net_expense, { digits: 0 })))));
    } else if (unusual.length) {
      unusual.forEach((pattern) => recurring.append(el('li', {},
        el('span', {}, titleFor(pattern.category)),
        el('b', {}, money(pattern.amount, { digits: 0 })))));
    } else {
      recurring.append(el('li', {}, 'Необычных и повторяющихся трат не найдено'));
    }
    renderPendingBadges();
  }

  /* -------------------------------- plan --------------------------------- */
  function renderPlan() {
    const goals = state.data.goals?.items || [];
    const host = $('#plan-goals');
    host.replaceChildren();
    if (!goals.length) host.append(el('p', { class: 'empty' }, 'Целей пока нет. Добавьте сумму и срок — Coin посчитает взнос.'));
    goals.forEach((goal) => {
      const progress = pct(goal.current_amount, goal.target_amount);
      const projection = goal.projection || {};
      const monthly = goal.monthly_contribution;
      host.append(el('div', { class: 'goal-card', 'data-goal-id': String(goal.id ?? '') },
        el('header', {}, el('h3', {}, goal.name),
          el('div', { class: 'actions' },
            el('button', { class: 'btn ghost small', type: 'button', onclick: () => openGoal(goal) }, 'Изменить'))),
        el('p', { class: 'amount' }, money(goal.current_amount, { digits: 0 }) + ' / ' + money(goal.target_amount, { digits: 0 })),
        el('div', { class: 'goal-bar' }, styled(el('i'), { width: progress + '%' })),
        el('div', { class: 'meta' },
          el('span', {}, goal.deadline ? 'до ' + dateShort(goal.deadline) : 'без срока'),
          el('span', {}, monthly ? money(monthly, { digits: 0 }) + ' / мес' : 'взнос не задан')),
        el('small', { class: 'fineprint' }, projection.status === 'available' || projection.months_needed
          ? `По текущему взносу: ${projection.months_needed ?? '?'} мес.`
          : 'Недостаточно данных для прогноза — задайте месячный взнос.')));
    });

    const position = state.data.cfo?.financial_position || {};
    const until = position.until_next_income || {};
    $('#horizon-title').textContent = position.next_income
      ? `Ближайший доход: ${dateShort(position.next_income)} · ${position.horizon_days} дн.`
      : 'Дата дохода не задана';
    const horizon = $('#horizon-list');
    horizon.replaceChildren();
    [['Пессимистичный', until.pessimistic], ['Базовый', until.base], ['Оптимистичный', until.optimistic]].forEach(([label, scenario]) => {
      if (!scenario) return;
      horizon.append(el('li', {},
        el('span', {}, label),
        styled(el('b'), Number(scenario.headroom) < 0 ? { color: 'var(--danger)' } : {}, money(scenario.headroom))));
    });
    $('#horizon-note').textContent = position.obligations_complete
      ? 'Обязательства учтены полностью.'
      : 'Обязательства могут быть неполными: задайте их в настройках, иначе сценарий оптимистичнее реальности.';

    const calendar = $('#plan-calendar');
    if (calendar) {
    calendar.replaceChildren();
    const profile = state.data.cfo?.profile || {};
    const incomes = el('div', { class: 'calendar-col' }, el('h3', {}, 'Доходы'));
    (profile.incomes || []).forEach((item) => incomes.append(el('li', {},
      el('span', {}, item.name), el('b', {}, money(item.amount, { digits: 0 })))));
    if (!(profile.incomes || []).length) incomes.append(el('li', {}, el('span', { class: 'muted' }, 'Не заданы')));
    const obligations = el('div', { class: 'calendar-col' }, el('h3', {}, 'Обязательства'));
    (profile.obligations || []).forEach((item) => obligations.append(el('li', {},
      el('span', {}, item.name), el('b', {}, money(item.amount, { digits: 0 })))));
    if (!(profile.obligations || []).length) obligations.append(el('li', {}, el('span', { class: 'muted' }, 'Не заданы')));
    const expected = el('div', { class: 'calendar-col' }, el('h3', {}, 'Ожидаемые поступления'));
    (state.data.analytics?.calendar?.expected_incomes || []).forEach((item) => expected.append(el('li', {},
      el('span', {}, `${item.name} · ${dateShort(item.date)}`), el('b', {}, money(item.amount, { digits: 0 })))));
    if (!(state.data.analytics?.calendar?.expected_incomes || []).length) expected.append(el('li', {}, el('span', { class: 'muted' }, 'Нет датированных ожиданий')));
    calendar.append(incomes, obligations, expected);
    }
  }

  function openGoal(goal = null) {
    const form = el('form', { class: 'stack' });
    const name = input({ name: 'name', required: true, maxlength: 100, placeholder: 'Например: ноутбук', value: goal?.name || '' });
    const target = input({ name: 'target_amount', inputmode: 'decimal', required: true, placeholder: '90000', value: goal?.target_amount || '' });
    const current = input({ name: 'current_amount', inputmode: 'decimal', required: true, placeholder: '0', value: goal?.current_amount || '0' });
    const monthly = input({ name: 'monthly_contribution', inputmode: 'decimal', placeholder: '5000', value: goal?.monthly_contribution || '' });
    const deadline = input({ name: 'deadline', type: 'date', value: goal?.deadline || '' });
    const status = el('select', { name: 'status' },
      el('option', { value: 'active' }, 'Активна'),
      el('option', { value: 'paused' }, 'Пауза'),
      el('option', { value: 'completed' }, 'Завершена'));
    if (goal?.status) status.value = goal.status;
    form.append(
      field('Название', name), field('Цель, ₽', target), field('Накоплено, ₽', current),
      field('Взнос в месяц, ₽', monthly), field('Дедлайн', deadline), field('Статус', status),
      el('p', { class: 'fineprint' }, 'Взнос — сколько вы готовы откладывать каждый месяц. Дедлайн и взнос влияют на прогноз.'));
    openSheet(goal ? 'Изменить цель' : 'Новая цель', form, [
      { label: 'Сохранить', kind: 'primary', run: async (close) => {
        if (!form.reportValidity()) return;
        const data = Object.fromEntries(new FormData(form).entries());
        try {
          const saved = await api('/api/cfo/goals', { method: 'POST', body: {
            name: data.name.trim(), target_amount: String(data.target_amount).replace(',', '.'),
            current_amount: String(data.current_amount || '0').replace(',', '.'),
            monthly_contribution: data.monthly_contribution ? String(data.monthly_contribution).replace(',', '.') : null,
            deadline: data.deadline || null, priority: goal?.priority ?? 3, status: data.status || 'active',
          } });
          toast('Цель сохранена', 'ok');
          if (saved?.auto_plan?.applied) {
            toast(`Qwen обновил ${saved.auto_plan.applied} лимитов под цель`, 'ok');
          }
          close();
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  /* ------------------------------ qwen chat ------------------------------ */
  function chatMarkdown(text) {
    let html = escapeHtml(text);
    html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/^[*•-]\s+(.+)$/gm, '• $1');
    html = html.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
    return `<p>${html}</p>`.replace(/<p><\/p>/g, '');
  }

  function renderQwenSessions() {
    const list = $('#chat-sessions');
    if (!list) return;
    list.replaceChildren();
    if (!state.chat.sessions.length) {
      list.append(el('p', { class: 'fineprint' }, 'Пока нет чатов. Создайте первый.'));
    }
    state.chat.sessions.forEach((session) => {
      const button = el('button', {
        type: 'button', class: session.id === state.chat.sessionId ? 'on' : '',
        onclick: () => openChat(session.id),
      }, el('b', {}, session.title || 'Без названия'),
         el('span', {}, `${session.messages || 0} сообщ. · ${ago(session.updated_at)}`));
      list.append(el('li', {}, button));
    });
    const count = state.chat.status?.memory?.confirmed ?? 0;
    $('#memory-count').textContent = String(count);
  }

  async function loadChatSessions() {
    const data = await api('/api/ai/sessions');
    state.chat.sessions = data.items || [];
    if (!state.chat.sessionId && state.chat.sessions.length) state.chat.sessionId = state.chat.sessions[0].id;
    renderQwenSessions();
  }

  async function loadChatStatus() {
    try {
      state.chat.status = await api('/api/qwen/status');
    } catch { state.chat.status = null; }
    const chip = $('#chat-model-chip');
    if (chip) chip.textContent = state.chat.status?.default_model || 'qwen3.7-plus';
    const note = $('#chat-note');
    const status = state.chat.status;
    if (note && status) {
      if (status.dispatch_allowed) {
        note.textContent = 'Qwen видит только ваши локальные финансы через read-only инструменты. Ключ провайдера не попадает в браузер.';
      } else {
        const unknown = status.open_reservations?.length || 0;
        note.textContent = unknown
          ? `Платный диалог на паузе: в журнале расходов ${plural(unknown, 'неизвестная попытка', 'неизвестные попытки', 'неизвестных попыток')} списания. Сверьте её в настройках AI — локальные расчёты в Coin AI работают всегда.`
          : 'Платный диалог сейчас недоступен по политике журнала расходов. Локальные расчёты в Coin AI работают всегда.';
      }
    }
    renderQwenSessions();
  }

  async function openChat(ident) {
    state.chat.sessionId = ident;
    state.chat.messages = [];
    renderChatThread();
    try {
      const data = await api(`/api/ai/sessions/${ident}/messages`);
      state.chat.messages = (data.items || []).map((row) => ({
        role: row.role, text: row.content, meta: row.model ? `${row.model} · ${money(row.cost_rub, { digits: 2 })}` : '',
      }));
      state.chat.session = data.session;
      $('#chat-title').textContent = data.session?.title || 'Чат';
      renderChatThread();
      renderQwenSessions();
    } catch (error) { toast(error.message, 'bad'); }
    closeChatDrawer();
  }

  function chatBubble(message) {
    const bubble = el('div', { class: `msg ${message.role === 'user' ? 'user' : 'coin'}` });
    bubble.innerHTML = chatMarkdown(message.text || '');
    if (message.tools?.length) {
      const tools = el('div', {});
      message.tools.forEach((item) => tools.append(el('span', { class: 'tool-chip' }, `⚙ ${item.name || item.detail || 'tool'}`)));
      bubble.append(tools);
    }
    if (message.actions?.length) {
      const chips = el('div', {});
      message.actions.forEach((action) => chips.append(el('span', { class: 'tool-chip action' },
        `→ ${ACTION_LABELS[action.type] || action.type}`)));
      bubble.append(chips);
    }
    const journalBlocked = message.code === 'journal_unknown_attempt' ||
      /AI ждёт сверки журнала/.test(String(message.text || ''));
    if (journalBlocked) {
      const actions = el('div', { class: 'chips' });
      actions.append(el('button', {
        type: 'button',
        onclick: () => openJournal(message.retry || ''),
      }, message.retry ? 'Сверить журнал и повторить' : 'Сверить журнал'));
      bubble.append(actions);
    }
    if (message.meta) bubble.append(el('span', { class: 'msg-meta' }, message.meta));
    return bubble;
  }

  function renderChatThread() {
    const host = $('#chat-thread');
    if (!host) return;
    host.replaceChildren();
    if (!state.chat.messages.length) {
      host.append(el('div', { class: 'chat-empty' },
        el('b', {}, 'Спросите Qwen о своих деньгах'),
        el('p', {}, 'Он вызовет read-only инструменты JARVIS и ответит по фактическим цифрам. Числа не выдумываются.'),
        el('div', { class: 'chips' }, ...[
          'Сколько можно потратить до следующего дохода?',
          'Где я переплачиваю?',
          'Покажи расходы на питание за 30 дней и предложи бюджет',
          'Помоги составить план накоплений',
        ].map((text) => el('button', { type: 'button', onclick: () => $('#chat-input').value = text }, text)))));
      return;
    }
    state.chat.messages.forEach((message) => host.append(chatBubble(message)));
    const last = state.chat.messages[state.chat.messages.length - 1];
    if (last && last.role === 'user' && !state.chat.streaming) {
      host.append(el('div', { class: 'chips' },
        el('button', {
          type: 'button',
          onclick: () => {
            const text = last.text;
            state.chat.messages.pop();
            sendChat(text);
          },
        }, 'Повторить запрос')));
    }
    host.scrollTop = host.scrollHeight;
  }

  function parseSse(block) {
    const event = /event: (.+)/.exec(block);
    const data = /data: (.+)/.exec(block);
    if (!event || !data) return null;
    try { return { name: event[1].trim(), payload: JSON.parse(data[1]) }; } catch { return null; }
  }

  async function sendChat(text) {
    if (state.chat.streaming) return;
    if (state.coinMode === 'local') {
      // Free deterministic answer, shown in the same thread and never stored.
      state.chat.messages.push({ role: 'user', text });
      renderChatThread();
      try {
        const answer = await api('/api/cfo/question', { method: 'POST', body: { message: text } });
        state.chat.messages.push({ role: 'assistant', text: answer.text || 'Нет ответа',
          meta: 'локальный расчёт · 0 ₽' });
      } catch (error) {
        state.chat.messages.push({ role: 'assistant', text: 'Не удалось посчитать: ' + error.message });
      }
      renderChatThread();
      return;
    }
    if (!state.chat.sessionId) {
      const created = await api('/api/ai/sessions', { method: 'POST', body: { title: text.slice(0, 60) } });
      state.chat.sessionId = created.session.id;
      state.chat.session = created.session;
      await loadChatSessions();
    }
    const ident = state.chat.sessionId;
    state.chat.messages.push({ role: 'user', text });
    const reply = { role: 'assistant', text: '', tools: [] };
    state.chat.messages.push(reply);
    state.chat.streaming = true;
    $('#chat-send').textContent = 'Стоп';
    $('#chat-send').type = 'button';
    renderChatThread();
    const thread = $('#chat-thread');
    const live = el('div', { class: 'msg coin' }, el('span', { class: 'typing' }, el('i'), el('i'), el('i')));
    thread.append(live);
    state.chat.controller = new AbortController();
    try {
      const response = await fetch(`/api/ai/sessions/${ident}/message`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-owner-csrf': await csrfToken() },
        credentials: 'same-origin',
        body: JSON.stringify({ text }),
        signal: state.chat.controller.signal,
      });
      if (!response.ok || !response.body) throw new ApiError('Qwen недоступен', response.status);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let meta = '';
      let actions = [];
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop() || '';
        for (const block of blocks) {
          const parsed = parseSse(block);
          if (!parsed) continue;
          if (parsed.name === 'text') {
            live.remove();
            reply.text += parsed.payload.chunk;
            thread.replaceChildren();
            state.chat.messages.slice(0, -1).forEach((message) => thread.append(chatBubble(message)));
            const current = el('div', { class: 'msg coin' });
            current.innerHTML = chatMarkdown(reply.text);
            thread.append(current);
            thread.scrollTop = thread.scrollHeight;
          } else if (parsed.name === 'tool') {
            reply.tools.push(parsed.payload);
          } else if (parsed.name === 'done') {
            reply.text = reply.text || parsed.payload.text || reply.text;
            meta = `${parsed.payload.model || 'qwen'} · ${money(parsed.payload.paid_cost_rub, { digits: 2 })}`;
            actions = parsed.payload.actions || [];
            if (parsed.payload.memory_suggestions?.length) {
              toast('Qwen предлагает запомнить — проверьте в «Память»', '');
            }
            if (parsed.payload.session?.title) $('#chat-title').textContent = parsed.payload.session.title;
          } else if (parsed.name === 'error') {
            const failure = new ApiError(parsed.payload.message || 'Ошибка Qwen', 502);
            failure.code = parsed.payload.code || '';
            throw failure;
          }
        }
      }
      reply.meta = meta;
      reply.actions = actions;
      if (actions.length) await applyActions(actions);
      if (!reply.text) reply.text = 'Пустой ответ. Попробуйте переформулировать.';
    } catch (error) {
      if (error.name === 'AbortError') {
        reply.text = reply.text || 'Остановлено.';
      } else {
        reply.text = `Не удалось получить ответ: ${error.message}. Бесплатный режим «Расчёт» работает всегда.`;
        reply.code = error.code || '';
        reply.retry = (state.chat.messages.filter((m) => m.role === 'user').slice(-1)[0] || {}).text || '';
      }
      live.remove();
    } finally {
      state.chat.streaming = false;
      $('#chat-send').textContent = 'Отправить';
      $('#chat-send').type = 'submit';
      state.chat.controller = null;
      renderChatThread();
      loadChatSessions().catch(() => {});
    }
  }

  function closeChatDrawer() {
    $('#chat-side')?.classList.remove('open');
    $('.chat-backdrop')?.remove();
  }

  function openChatDrawer() {
    const side = $('#chat-side');
    if (!side) return;
    side.classList.add('open');
    const backdrop = el('div', { class: 'chat-backdrop', onclick: closeChatDrawer });
    document.body.append(backdrop);
  }

  async function openMemory() {
    let items = [];
    try {
      items = (await api('/api/ai/memory')).items || [];
    } catch (error) { toast(error.message, 'bad'); return; }
    const body = el('div', { class: 'stack' });
    const form = el('form', { class: 'inline-form' },
      field('Новая запись', input({ name: 'text', maxlength: 400, placeholder: 'Например: не тратить больше 2000 ₽ на доставку' })),
      el('button', { class: 'btn primary', type: 'submit' }, 'Запомнить'));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = new FormData(form).get('text');
      if (!String(text || '').trim()) return;
      try {
        await api('/api/ai/memory', { method: 'POST', body: { text, kind: 'preference' } });
        toast('Запомнено', 'ok');
        closeSheet();
        openMemory();
      } catch (error) { toast(error.message, 'bad'); }
    });
    body.append(form);
    const list = el('div', { class: 'stack' });
    if (!items.length) list.append(el('p', { class: 'fineprint' }, 'Пока ничего не подтверждено. Qwen использует только подтверждённые записи.'));
    items.forEach((item) => {
      const actions = el('div', { class: 'memory-actions' });
      if (item.status === 'pending') {
        actions.append(el('button', {
          class: 'btn ghost small', type: 'button',
          onclick: async () => {
            await api(`/api/ai/memory/${item.id}/decide`, { method: 'POST', body: { status: 'confirmed' } });
            closeSheet(); openMemory();
          },
        }, 'Подтвердить'));
        actions.append(el('button', {
          class: 'btn ghost small', type: 'button',
          onclick: async () => {
            await api(`/api/ai/memory/${item.id}/decide`, { method: 'POST', body: { status: 'rejected' } });
            closeSheet(); openMemory();
          },
        }, 'Отклонить'));
      }
      actions.append(el('button', {
        class: 'btn ghost small', type: 'button',
        onclick: async () => {
          await api(`/api/ai/memory/${item.id}`, { method: 'DELETE' });
          closeSheet(); openMemory();
        },
      }, 'Удалить'));
      list.append(el('div', { class: 'memory-item' },
        el('div', {}, el('b', {}, item.text), el('small', {}, `${item.kind} · ${item.status === 'confirmed' ? 'используется' : item.status === 'pending' ? 'ждёт решения' : 'отклонено'}`)),
        actions));
    });
    body.append(list);
    openSheet('Память Qwen', body);
  }

  async function searchChats(query) {
    const host = $('#chat-search-results');
    if (!host) return;
    if (!query.trim()) { host.hidden = true; host.replaceChildren(); return; }
    try {
      const data = await api(`/api/ai/search?q=${encodeURIComponent(query)}`);
      host.replaceChildren();
      (data.items || []).forEach((hit) => host.append(el('button', {
        type: 'button', onclick: () => openChat(hit.session_id),
      }, el('b', {}, hit.title || 'Чат'), `${hit.role === 'user' ? 'Вы' : 'Qwen'}: ${hit.content.slice(0, 90)}`)));
      if (!(data.items || []).length) host.append(el('p', { class: 'fineprint' }, 'Ничего не найдено.'));
      host.hidden = false;
    } catch (error) { toast(error.message, 'bad'); }
  }

  async function openJournal(retryText = '') {
    let data;
    try {
      data = await api('/api/qwen/journal');
    } catch (error) { toast(error.message, 'bad'); return; }
    const rows = data.open || [];
    const body = el('div', { class: 'stack' });
    body.append(el('p', { class: 'body-text' },
      'Открытая запись блокирует платные ответы, пока не сверена. Сверка списывает сумму по худшему сценарию (резерв), а не обнуляет её. Это решение владельца.'));
    if (!rows.length) {
      body.append(el('p', { class: 'fineprint' }, 'Открытых записей нет — платный диалог доступен.'));
      openSheet('Журнал AI', body);
      return;
    }
    rows.forEach((row) => {
      const form = el('form', { class: 'stack' });
      form.append(
        el('div', { class: 'kv' },
          el('div', {}, el('dt', {}, 'Запись'), el('dd', {}, row.request_id.slice(0, 12) + '…')),
          el('div', {}, el('dt', {}, 'Статус'), el('dd', {}, row.status)),
          el('div', {}, el('dt', {}, 'Резерв'), el('dd', {}, money(row.reserved_rub))),
          el('div', {}, el('dt', {}, 'Создана'), el('dd', {}, dateFull(row.created_at)))),
        field('Фактическая сумма, ₽ (по умолчанию резерв)',
          input({ name: 'actual_rub', inputmode: 'decimal', value: row.reserved_rub, maxlength: 20 })),
        field('Основание сверки', input({ name: 'note', maxlength: 300, value: 'Провайдер не подтвердил результат; подтверждаю худший сценарий', placeholder: 'например: провайдер не выставил счёт, подтверждаю худший сценарий' })));
      const submit = el('button', { class: 'btn primary', type: 'submit' }, 'Подтвердить сверку');
      form.append(submit);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data2 = Object.fromEntries(new FormData(form).entries());
        if (String(data2.note || '').trim().length < 10) { toast('Опишите основание (10+ символов)', 'bad'); return; }
        submit.disabled = true;
        try {
          const result = await api(`/api/qwen/journal/${row.request_id}/settle`, {
            method: 'POST',
            body: { confirm: true, actual_rub: String(data2.actual_rub || '').trim() || null, note: data2.note },
          });
          toast(`Сверено: ${money(result.amount_rub)}`, 'ok');
          closeSheet();
          await loadChatStatus();
          renderSettings();
          if (retryText) setTimeout(() => sendChat(retryText), 350);
        } catch (error) {
          toast(error.message, 'bad');
          submit.disabled = false;
        }
      });
      body.append(el('div', { class: 'memory-item' }, form));
    });
    openSheet('Журнал AI', body);
  }

  /* --------------------------- qwen actions ------------------------------ */
  const ACTION_LABELS = {
    navigate: 'переход', chart: 'график', filter: 'фильтр операций',
    transaction: 'операция', goal: 'цель', draft_budget: 'черновик бюджета', explain: 'объяснение',
  };

  function applyOperationFilters(kind, days, category) {
    state.filters.kind = kind;
    state.filters.days = days;
    state.filters.category = category;
    const seg = $('#op-kind');
    if (seg) $$('button', seg).forEach((button) => button.classList.toggle('on', button.dataset.kind === kind));
    const period = $('#op-period');
    if (period) period.value = String(days);
    const cat = $('#op-category');
    if (cat) cat.value = category;
    state.loaded.history = false;
    return reloadOperations();
  }

  async function applyActions(actions) {
    if (!Array.isArray(actions) || !actions.length) return;
    const applied = [];
    for (const action of actions.slice(0, 5)) {
      const type = String(action && action.type || '');
      try {
        if (type === 'navigate' && PAGES[action.page]) {
          location.hash = '#/' + action.page;
          applied.push(PAGES[action.page].title);
        } else if (type === 'chart') {
          const days = [7, 30, 90].includes(Number(action.period)) ? Number(action.period) : 30;
          location.hash = '#/dashboard';
          await loadOverview(days);
          applied.push(`график ${days} дн.`);
        } else if (type === 'filter') {
          const days = ({ '7': 7, '30': 30, '90': 90, month: 30, prev_month: 90 })[String(action.date_range)] || 30;
          const kind = ['all', 'expense', 'income', 'transfer', 'unknown'].includes(action.kind) ? action.kind : 'all';
          const category = /^[a-z_][a-z0-9_:]{0,79}$/.test(String(action.category || '')) ? action.category : '';
          location.hash = '#/operations';
          await applyOperationFilters(kind, days, category);
          applied.push('фильтр');
        } else if (type === 'transaction' && Number(action.id) > 0) {
          location.hash = '#/operations';
          if (!state.loaded.history) { await loadHistory(true); state.loaded.history = true; }
          if (action.merchant) {
            $('#op-search').value = '';
            await applyOperationFilters('all', 90, '');
          }
          const row = state.history.items.find((item) => item.id === Number(action.id));
          if (row) { openOperation(row); applied.push('операция'); }
          else { toast('Операция вне текущего периода — откройте историю', ''); }
        } else if (type === 'goal' && Number(action.id) > 0) {
          location.hash = '#/plan';
          await new Promise((resolve) => setTimeout(resolve, 120));
          const card = document.querySelector(`[data-goal-id="${Number(action.id)}"]`);
          if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.classList.add('flash');
            setTimeout(() => card.classList.remove('flash'), 1800);
            applied.push('цель');
          }
        } else if (type === 'draft_budget') {
          const category = String(action.category || '');
          const amount = String(action.amount || '');
          if (!/^[a-z_][a-z0-9_:]{0,79}$/.test(category) || !Number(amount.replace(',', '.'))) continue;
          location.hash = '#/budget';
          openDraftBudget(category, amount);
          applied.push('черновик бюджета');
        } else if (type === 'explain') {
          location.hash = '#/dashboard';
          openSheet('Почему так', el('p', { class: 'body-text' }, String(action.text || '')), []);
          applied.push('объяснение');
        }
      } catch (error) { /* one failed action never blocks the others */ }
    }
    if (applied.length) toast(`Qwen: ${applied.join(', ')}`, '');
  }

  function openDraftBudget(category, amount) {
    const form = el('form', { class: 'stack' },
      field('Категория', el('div', { class: 'body-text' }, titleFor(category))),
      field('Лимит в месяц, ₽', input({ name: 'amount', inputmode: 'decimal', value: amount, required: true })));
    openSheet('Черновик бюджета от Qwen', form, [
      { label: 'Сохранить лимит', kind: 'primary', run: async (close) => {
        if (!form.reportValidity()) return;
        const value = String(new FormData(form).get('amount') || '').replace(',', '.');
        try {
          await api('/api/cfo/category-budget', { method: 'PUT', body: { category, monthly_limit: value } });
          toast('Лимит сохранён', 'ok');
          close();
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  /* --------------------------- devices / passkeys ------------------------ */
  const b64url = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const fromB64url = (value) => {
    const pad = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  };
  const passkeySupported = () => Boolean(window.PublicKeyCredential && navigator.credentials
    && window.isSecureContext);

  async function restoreDeviceSession() {
    try {
      const response = await fetch('/auth/device', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'content-type': 'application/json' }, body: '{}',
      });
      return response.ok;
    } catch { return false; }
  }

  async function loadDevices() {
    const list = $('#devices-list');
    if (!list) return;
    try {
      const data = await api('/api/auth/devices');
      const items = data.items || [];
      $('#device-chip').textContent = items.length
        ? plural(items.length, 'устройство', 'устройства', 'устройств')
        : 'нет устройств';
      list.replaceChildren();
      if (!items.length) {
        list.append(el('li', {}, el('span', { class: 'muted' }, 'Пока нет доверенных устройств')));
      }
      items.forEach((device) => {
        const active = !device.revoked_at && !device.expired;
        list.append(el('li', {},
          el('div', { class: 'name' },
            el('b', {}, device.label || 'Устройство'),
            el('span', {}, `${active ? 'до ' + dateShort(device.expires_at) : device.revoked_at ? 'отключено' : 'истекло'}`
              + (device.passkeys ? ' · Face ID' : '') + ` · вход ${ago(device.last_seen_at)}`)),
          el('span', { class: 'amt' }, device.passkeys ? '🔐' : ''),
          el('button', {
            class: 'del', type: 'button', 'aria-label': 'Отключить устройство',
            onclick: async () => {
              try {
                await api(`/api/auth/devices/${device.id}/revoke`, { method: 'POST', body: {} });
                toast('Устройство отключено', 'ok');
                await loadDevices();
              } catch (error) { toast(error.message, 'bad'); }
            },
          }, icon('i-x'))));
      });
      const supported = passkeySupported();
      $('#enable-passkey').hidden = !supported;
      $('#device-status').textContent = supported
        ? 'Face ID доступен на этом устройстве.'
        : 'Face ID доступен только по HTTPS в Safari (iPhone).';
    } catch (error) {
      $('#device-status').textContent = error.message;
    }
  }

  async function registerPasskey() {
    const button = $('#enable-passkey');
    button.disabled = true;
    try {
      const begin = await api('/api/auth/passkey/register/begin', { method: 'POST', body: {} });
      const publicKey = begin.options;
      publicKey.challenge = fromB64url(publicKey.challenge);
      publicKey.user.id = fromB64url(publicKey.user.id);
      publicKey.excludeCredentials = (publicKey.excludeCredentials || [])
        .map((item) => ({ ...item, id: fromB64url(item.id) }));
      const created = await navigator.credentials.create({ publicKey });
      const credential = {
        id: created.id, rawId: b64url(created.rawId), type: created.type,
        response: {
          clientDataJSON: b64url(created.response.clientDataJSON),
          attestationObject: b64url(created.response.attestationObject),
          transports: created.response.getTransports ? created.response.getTransports() : [],
        },
      };
      await api('/api/auth/passkey/register/complete', { method: 'POST',
        body: { challenge_id: begin.challenge_id, credential, label: 'Face ID' } });
      toast('Face ID включён', 'ok');
      await loadDevices();
    } catch (error) {
      toast(`Face ID не включён: ${error.message}`, 'bad');
    } finally {
      button.disabled = false;
    }
  }

  async function openPairSheet() {
    let start;
    try {
      start = await api('/api/auth/pair/start', { method: 'POST', body: {} });
    } catch (error) { toast(error.message, 'bad'); return; }
    const statusLine = el('p', { class: 'body-text', role: 'status' }, 'Отсканируйте QR камерой iPhone.');
    const approve = el('button', { class: 'btn primary', type: 'button', hidden: true }, 'Подтвердить устройство');
    const qr = el('div', { class: 'pair-qr' });
    qr.innerHTML = start.svg;
    const body = el('div', { class: 'stack' }, qr, statusLine,
      el('p', { class: 'fineprint' }, start.url), approve);
    openSheet('Подключить устройство', body, []);
    let timer = 0;
    let done = false;
    approve.addEventListener('click', async () => {
      try {
        await api(`/api/auth/pair/${start.id}/approve`, { method: 'POST', body: {} });
        approve.hidden = true;
        statusLine.textContent = 'Подтверждено. Ждём, пока iPhone завершит подключение…';
      } catch (error) { toast(error.message, 'bad'); }
    });
    const poll = async () => {
      if (done) return;
      try {
        const state = await api(`/api/auth/pair/${start.id}`);
        if (state.status === 'approved' && state.used) {
          done = true;
          statusLine.textContent = 'Устройство подключено. Можно закрыть окно.';
          approve.hidden = true;
          await loadDevices();
          clearInterval(timer);
          return;
        }
        if (state.status === 'approved') {
          approve.hidden = true;
          statusLine.textContent = 'Подтверждено. Ждём iPhone…';
        } else if (state.opened) {
          approve.hidden = false;
          statusLine.textContent = 'Телефон на связи. Подтвердите устройство здесь.';
        }
      } catch { clearInterval(timer); }
    };
    timer = setInterval(poll, 2000);
    setTimeout(() => { if (!done) clearInterval(timer); }, 90000);
  }

  /* --------------------------- autopilot UI ------------------------------ */
  async function openBudgetPlan() {
    let plan;
    try { plan = await api('/api/cfo/budget-plan'); }
    catch (error) { toast(error.message, 'bad'); return; }
    if (plan.status !== 'available') { toast('Пока мало истории для плана', 'bad'); return; }
    const rows = el('div', { class: 'stack plan-list' });
    plan.categories.slice(0, 12).forEach((item) => rows.append(el('div', { class: 'memory-item' },
      el('div', {},
        el('b', {}, item.title),
        el('small', {}, item.cut_percent
          ? `сейчас ≈ ${money(item.average, { digits: 0 })} · срез ${item.cut_percent}%`
          : `сейчас ≈ ${money(item.average, { digits: 0 })} · без изменений`)),
      el('b', {}, money(item.suggested, { digits: 0 })))));
    const months = plan.goal?.months_to_goal;
    const body = el('div', { class: 'stack' },
      el('p', { class: 'body-text' }, plan.goal_note || plan.note),
      el('p', { class: 'body-text' },
        `Итого лимитов: ${money(plan.planned_total, { digits: 0 })}.`
        + (months ? ` При таком плане цель примерно за ${months} мес.` : '')),
      rows,
      el('p', { class: 'fineprint' }, plan.note));
    openSheet('План на месяц', body, [
      { label: 'Применить лимиты', kind: 'primary', run: async (close) => {
        try {
          const result = await api('/api/cfo/budget-plan/apply', {
            method: 'POST', body: { categories: plan.categories } });
          toast(`Лимиты сохранены: ${result.applied}`, 'ok');
          close();
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  async function runQueueAI() {
    const button = $('#queue-ai');
    button.disabled = true;
    try {
      const result = await api('/api/ai/autopilot/classify', {
        method: 'POST', body: { classify_limit: 3 } });
      if (result.status === 'disabled') { toast('Автопилот выключен в настройках', 'warn'); return; }
      toast(result.applied ? `Qwen разобрал операций: ${result.applied}` : 'Понятных категорий не нашлось',
        result.applied ? 'ok' : '');
      await refreshAfterEdit();
    } catch (error) { toast(error.message, 'bad'); }
    finally { button.disabled = false; }
  }

  async function loadAutopilot() {
    try {
      const data = await api('/api/ai/autopilot');
      const settings = data.settings || {};
      if ($('#autopilot-classify')) $('#autopilot-classify').checked = Boolean(settings.classify);
      if ($('#autopilot-budget')) $('#autopilot-budget').checked = Boolean(settings.budget);
    } catch { /* defaults stay */ }
  }

  async function maybeAutopilot() {
    try {
      const settings = (await api('/api/ai/autopilot')).settings || {};
      if (!settings.classify) return;
      const KEY = 'jarvis.autopilot.at';
      const last = Number(sessionStorage.getItem(KEY) || 0);
      if (Date.now() - last < 15 * 60 * 1000) return;
      if (!(Number(state.data.pendingUnknown) > 0)) return;
      sessionStorage.setItem(KEY, String(Date.now()));
      const result = await api('/api/ai/autopilot/classify', { method: 'POST', body: {} });
      if (result.applied) {
        toast('Qwen сам разобрал операций: ' + result.applied, 'ok');
        await refreshAfterEdit();
      }
    } catch { /* autopilot never blocks the app */ }
  }

  /* ------------------------------ settings ------------------------------- */
  function renderSettings() {
    const cfo = state.data.cfo || {};
    const bank = cfo.bank;
    const runtime = state.data.runtime || {};
    const sync = runtime.jobs?.sync || {};
    const bankState = $('#bank-state');
    const fresh = Boolean(cfo.bank_fresh);
    bankState.className = `chip ${fresh ? 'ok' : bank ? 'warn' : 'bad'}`;
    bankState.replaceChildren(el('i', { class: 'dot' }), fresh ? 'свежие данные' : bank ? 'сохранённый снимок' : 'не подключён');
    const kv = $('#bank-kv');
    kv.replaceChildren();
    const rows = [
      ['Последнее чтение', bank ? dateFull(bank.observed_at) : 'нет'],
      ['Счёта', bank ? String(bank.accounts?.length || 0) : '—'],
      ['История', coverageLabel(cfo.history_coverage)],
      ['На проверке', bank ? String(bank.held_operations || 0) : '—'],
      ['Последняя попытка', sync.at ? `${ago(sync.at)} · ${syncLabel(sync.status)}` : '—'],
    ];
    rows.forEach(([label, value]) => kv.append(el('div', {}, el('dt', {}, label), el('dd', {}, value))));
    const lastRefresh = cfo.last_refresh || {};
    $('#bank-note').textContent = (sync.status === 'auth_required' || lastRefresh.status === 'auth_required')
      ? 'Сессия Сбера не подтверждена. Введите PIN — он передаётся только локальному SDK и не сохраняется в базе.'
      : 'Чтение только остатков и истории. Платежи и переводы недоступны по дизайну.';
    $('#pin-form').hidden = !(sync.status === 'auth_required' || sync.code === 'pin_required'
      || lastRefresh.status === 'auth_required');

    const settings = runtime.settings || {};
    const form = $('#sync-form');
    if (settings.sync_minutes) form.sync_minutes.value = String(settings.sync_minutes);
    form.bank_enabled.checked = Boolean(settings.bank_enabled);
    form.alerts_enabled.checked = Boolean(settings.alerts_enabled);
    form.daily_enabled.checked = Boolean(settings.daily_enabled);
    $('#sync-note').textContent = runtime.runtime_active
      ? 'Фоновый планировщик запущен в этом процессе.'
      : 'Процесс запущен в режиме web-only: банк читается по кнопке, фоновые задания выключены.';

    const profile = cfo.profile || {};
    const cash = profile.cash || {};
    const cashForm = $('#cash-form');
    cashForm.cash_low.value = cash.low ?? '';
    cashForm.cash_high.value = cash.high ?? '';
    cashForm.cash_source.value = cash.source || 'owner_estimate';
    cashForm.reserve.value = profile.reserve?.low ?? '';
    cashForm.protected.value = profile.protected?.low ?? '';

    renderProfileList($('#income-list'), profile.incomes || [], 'income');
    renderProfileList($('#obligation-list'), profile.obligations || [], 'obligation');
    $('#obligations-complete').checked = Boolean(profile.obligations_complete);

    const motionToggle = $('#motion-toggle');
    const glassToggle = $('#glass-toggle');
    if (motionToggle) motionToggle.checked = document.documentElement.dataset.motion !== 'off';
    if (glassToggle) glassToggle.checked = document.documentElement.dataset.glass !== 'off';
    $$('#bg-mode button').forEach((button) =>
      button.classList.toggle('on', button.dataset.bg === bg.mode));
    loadDevices();
    loadAutopilot();

    const cognitive = state.data.cognitive || {};
    const qwen = state.chat.status || null;
    $('#ai-state').className = `chip ${qwen?.dispatch_allowed ? 'ok' : 'warn'}`;
    $('#ai-state').textContent = qwen?.dispatch_allowed ? 'диалог доступен' : 'диалог на паузе';
    const aiKv = $('#ai-kv');
    aiKv.replaceChildren();
    [
      ['Провайдер', cognitive.model || qwen?.provider || 'не настроен'],
      ['Месячный лимит', qwen?.budget?.hard_monthly_limit_rub ? money(qwen.budget.hard_monthly_limit_rub, { digits: 0 }) : (cognitive.monthly_limit_rub ? money(cognitive.monthly_limit_rub, { digits: 0 }) : '—')],
      ['Осталось', qwen?.budget?.remaining_rub ? money(qwen.budget.remaining_rub) : '—'],
      ['Неизвестные попытки', String(qwen?.open_reservations?.length ?? 0)],
      ['Облачный чат', cognitive.cloud_ai_chat ? 'разрешён' : 'выключен'],
      ['Автоподмена модели', cognitive.automatic_model_fallback ? 'да' : 'нет'],
    ].forEach(([label, value]) => aiKv.append(el('div', {}, el('dt', {}, label), el('dd', {}, value))));
    const paused = qwen && !qwen.dispatch_allowed;
    $('#ai-hint').textContent = paused
      ? 'Платный диалог приостановлен: в журнале расходов есть незавершённая попытка. Сверка журнала — решение владельца; лимиты и разрешения не меняются автоматически.'
      : 'Ключ провайдера хранится локально и не отображается. Без AI локальные расчёты и очередь категорий продолжают работать.';
    const action = $('#ai-journal-btn');
    if (action) {
      action.hidden = !(qwen?.open_reservations?.length);
      action.textContent = qwen?.open_reservations?.length === 1
        ? 'Сверить 1 запись журнала'
        : `Сверить ${qwen?.open_reservations?.length || 0} записи журнала`;
    }
  }

  function renderProfileList(host, items, kind) {
    host.replaceChildren();
    if (!items.length) host.append(el('li', {}, el('span', { class: 'muted' }, 'Не заданы')));
    items.forEach((item, index) => host.append(el('li', {},
      el('div', { class: 'name' }, el('b', {}, item.name), el('span', {}, frequencyLabel(item))),
      el('span', { class: 'amt' }, money(item.amount, { digits: 0 })),
      el('button', { class: 'del', type: 'button', 'aria-label': 'Удалить', onclick: () => removeProfileItem(kind, index) },
        icon('i-x')))));
  }

  function frequencyLabel(item) {
    const map = { once: 'разово', daily: 'ежедневно', weekly: 'еженедельно', monthly: 'ежемесячно', interval: `каждые ${item.interval_days || 14} дн.` };
    const certainty = { confirmed: 'подтверждено', expected: 'ожидается', estimate: 'оценка', candidate: 'вариант' };
    return `${map[item.frequency] || item.frequency} · ${certainty[item.certainty] || item.certainty}`;
  }

  async function saveProfile(patch) {
    const current = state.data.cfo?.profile || { version: 2, incomes: [], obligations: [], other_assets: [] };
    const next = {
      version: 2,
      cash: current.cash || null,
      other_assets: current.other_assets || [],
      protected: current.protected || null,
      reserve: current.reserve || null,
      incomes: current.incomes || [],
      obligations: current.obligations || [],
      obligations_complete: Boolean(current.obligations_complete),
      ...patch,
    };
    await api('/api/cfo/profile', { method: 'PUT', body: next });
    toast('Профиль сохранён', 'ok');
    await refreshAfterEdit();
  }

  async function removeProfileItem(kind, index) {
    const profile = state.data.cfo?.profile || {};
    const key = kind === 'income' ? 'incomes' : 'obligations';
    const items = (profile[key] || []).slice();
    items.splice(index, 1);
    try { await saveProfile({ [key]: items }); } catch (error) { toast(error.message, 'bad'); }
  }

  function openProfileItem(kind) {
    const form = el('form', { class: 'stack' });
    const name = input({ name: 'name', required: true, maxlength: 100, placeholder: kind === 'income' ? 'Стипендия' : 'Аренда' });
    const amount = input({ name: 'amount', inputmode: 'decimal', required: true, placeholder: '4200' });
    const frequency = el('select', { name: 'frequency' },
      el('option', { value: 'once' }, 'Разово'),
      el('option', { value: 'monthly' }, 'Ежемесячно'),
      el('option', { value: 'weekly' }, 'Еженедельно'),
      el('option', { value: 'interval' }, 'Каждые N дней'));
    frequency.value = kind === 'income' ? 'monthly' : 'monthly';
    const interval = input({ name: 'interval_days', type: 'number', min: 1, max: 366, value: 14 });
    const nextDate = input({ name: 'next_date', type: 'date' });
    const certainty = el('select', { name: 'certainty' },
      el('option', { value: 'confirmed' }, 'Подтверждено'),
      el('option', { value: 'expected' }, 'Ожидается'),
      el('option', { value: 'estimate' }, 'Оценка'));
    form.append(field('Название', name), field('Сумма, ₽', amount), field('Периодичность', frequency),
      field('Каждые N дней', interval), field('Дата следующего платежа', nextDate), field('Надёжность', certainty));
    openSheet(kind === 'income' ? 'Новый доход' : 'Новый платёж', form, [
      { label: 'Добавить', kind: 'primary', run: async (close) => {
        if (!form.reportValidity()) return;
        const data = Object.fromEntries(new FormData(form).entries());
        const profile = state.data.cfo?.profile || {};
        const key = kind === 'income' ? 'incomes' : 'obligations';
        const item = {
          name: data.name.trim(), amount: String(data.amount).replace(',', '.'),
          category: kind === 'income' ? 'family_income' : 'bills',
          frequency: data.frequency, interval_days: Number(data.interval_days || 14),
          next_date: data.next_date || null, certainty: data.certainty,
          enabled: true, source: 'owner_estimate',
        };
        try {
          await saveProfile({ [key]: [...(profile[key] || []), item] });
          close();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  /* ------------------------------ quick add ------------------------------ */
  function openQuickAdd(kind = 'expense') {
    let type = kind;
    const form = el('form', { class: 'stack' });
    const amount = input({ name: 'amount', inputmode: 'decimal', required: true, placeholder: '0', autocomplete: 'off' });
    amount.style.fontSize = '24px';
    amount.style.height = '52px';
    amount.style.fontWeight = '600';
    const toggle = el('div', { class: 'type-toggle' },
      el('button', { type: 'button', class: type === 'expense' ? 'on' : '', onclick: () => setType('expense') }, 'Расход'),
      el('button', { type: 'button', class: type === 'income' ? 'on' : '', onclick: () => setType('income') }, 'Доход'));
    const quick = el('div', { class: 'quick-amount' },
      ...[100, 300, 500, 1000].map((value) => el('button', {
        type: 'button', onclick: () => { amount.value = String(value); },
      }, String(value))));
    const merchant = input({ name: 'merchant', maxlength: 120, placeholder: 'Пятёрочка' });
    const description = input({ name: 'description', maxlength: 300, placeholder: 'необязательно' });
    const date = input({ name: 'tx_date', type: 'date' });
    date.value = new Date().toISOString().slice(0, 10);
    const categoryHost = el('div', { class: 'cat-picker' });
    let category = type === 'expense' ? 'food_basic' : 'stipend';

    function drawCategories() {
      categoryHost.replaceChildren();
      const groups = pickerGroups(type === 'income');
      Object.entries(groups).forEach(([bucket, items]) => {
        const grid = el('div', { class: 'cat-grid' });
        items.forEach((item) => {
          const button = el('button', {
            type: 'button', class: item.category === category ? 'on' : '',
            onclick: () => {
              category = item.category;
              $$('.cat-grid button', categoryHost).forEach((b) => b.classList.remove('on'));
              button.classList.add('on');
            },
          }, item.title);
          grid.append(button);
        });
        categoryHost.append(el('div', { class: 'cat-group' }, el('h4', {}, bucket), grid));
      });
    }
    function setType(next) {
      type = next;
      $$('button', toggle).forEach((b, index) => b.classList.toggle('on', (index === 0) === (next === 'expense')));
      category = next === 'expense' ? 'food_basic' : 'stipend';
      drawCategories();
    }
    drawCategories();

    form.append(el('div', {}, toggle), field('Сумма, ₽', amount), quick, field('Где', merchant),
      field('Комментарий', description), field('Дата', date), el('p', { class: 'eyebrow' }, 'Категория'), categoryHost);
    openSheet('Записать операцию', form, [
      { label: 'Сохранить', kind: 'primary', run: async (close) => {
        if (!form.reportValidity()) return;
        const data = Object.fromEntries(new FormData(form).entries());
        try {
          const result = await api('/api/cfo/expenses', { method: 'POST', body: {
            amount: String(data.amount).replace(',', '.'), kind: type, category,
            merchant: data.merchant || '', description: data.description || '',
            tx_date: data.tx_date || null, request_id: randomId(),
          } });
          toast(result.status === 'saved' ? 'Записано' : 'Такая запись уже есть', result.status === 'saved' ? 'ok' : '');
          close();
          await refreshAfterEdit();
        } catch (error) { toast(error.message, 'bad'); }
      } },
    ]);
  }

  /* -------------------------------- router ------------------------------- */
  const PAGES = {
    dashboard: { title: 'Обзор', sub: () => 'Ваши деньги, обязательства и план на месяц' },
    operations: { title: 'Операции', sub: () => 'История, категории и очередь подтверждений' },
    budget: { title: 'Бюджет', sub: () => 'Лимиты по категориям и повторяющиеся платежи' },
    plan: { title: 'Планирование', sub: () => 'Цели, прогноз до дохода и крупные покупки' },
    qwen: { title: 'Qwen', sub: () => 'Диалог с общей памятью и read-only доступом к финансам' },
    settings: { title: 'Настройки', sub: () => 'Подключения, синхронизация и приватность' },
  };

  function currentRoute() {
    const hash = location.hash.replace(/^#\/?/, '');
    return PAGES[hash] ? hash : 'dashboard';
  }

  function swapPage(route) {
    $$('.page').forEach((page) => { page.hidden = page.dataset.page !== route; });
    $$('[data-nav]').forEach((link) => {
      if (link.dataset.nav === route) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    $('#page-title').textContent = PAGES[route].title;
    $('#page-sub').textContent = PAGES[route].sub();
    window.scrollTo({ top: 0, behavior: 'auto' });
    renderCurrentPage();
  }

  async function navigate() {
    const route = currentRoute();
    // View Transitions give a native, compositor-friendly page cross-fade.
    if (document.startViewTransition && document.documentElement.dataset.motion !== 'off') {
      try { document.startViewTransition(() => swapPage(route)); }
      catch { swapPage(route); }
    } else {
      swapPage(route);
    }
    if (route === 'operations' && !state.loaded.history) {
      try {
        await loadHistory(true);
        state.loaded.history = true;
        renderOperations(true);
      } catch (error) { toast(error.message, 'bad'); }
    }
    if (route === 'qwen' && !state.loaded.chat) {
      state.loaded.chat = true;
      try {
        await Promise.all([loadChatSessions(), loadChatStatus()]);
        if (state.chat.sessionId) await openChat(state.chat.sessionId);
        else renderChatThread();
      } catch (error) { toast(error.message, 'bad'); }
    }
    if ((route === 'settings' || route === 'qwen') && !state.chat.status) {
      try { await loadChatStatus(); } catch { /* local UI still renders */ }
      if (route === 'settings') renderSettings();
    }
  }

  function renderCurrentPage() {
    const route = currentRoute();
    if (!state.loaded.core) return;
    if (route === 'dashboard') renderDashboard();
    if (route === 'budget') renderBudget();
    if (route === 'plan') renderPlan();
    if (route === 'qwen') renderQwenSessions();
    if (route === 'settings') renderSettings();
    if (route === 'operations') renderOperations(true);
    renderRailSync();
  }

  function renderRailSync() {
    const bank = state.data.cfo?.bank;
    const sync = state.data.runtime?.jobs?.sync || {};
    const host = $('#rail-sync');
    host.replaceChildren(
      el('b', {}, 'Сбер'),
      el('span', {}, bank ? `${ago(bank.observed_at)}` : 'нет снимка'),
      el('br'),
      el('span', {}, sync.status === 'auth_required' ? 'нужен PIN' : sync.status === 'updated' ? 'синхронизировано' : 'ожидание'));
  }

  /* ------------------------------- actions ------------------------------- */
  async function refreshBank(pin = null) {
    const button = $('#bank-refresh');
    button.disabled = true;
    try {
      const result = pin
        ? await api('/api/cfo/submit-pin', { method: 'POST', body: { pin } })
        : await api('/api/cfo/refresh', { method: 'POST', body: {} });
      if (result.status === 'connected' || result.status === 'updated') {
        toast('Сбер прочитан', 'ok');
        $('#pin-form').hidden = true;
        banner('');
      } else if (result.status === 'auth_required' || result.code === 'pin_required') {
        $('#pin-form').hidden = false;
        toast('Нужен повторный вход в Сбер', 'warn');
        banner('Сессия Сбера не подтверждена. Введите PIN в настройках; если не поможет — нужен локальный вход через launcher.', 'warn');
      } else if (result.status === 'busy') {
        toast('Синхронизация уже идёт', '');
      } else if (result.status === 'cooling_down') {
        const minutes = Math.max(1, Math.ceil((result.retry_after_seconds || 60) / 60));
        toast(`Повторный вход в Сбер будет доступен через ${minutes} мин — защита от блокировки`, 'warn');
        banner('Сбер ограничивает частые попытки входа. Следующая попытка — после паузы; старый снимок остаётся честно помеченным.', 'warn');
      } else if (result.code === 'MCPUnavailable' || result.code === 'not_configured') {
        toast('Сбер недоступен: проверьте подключение MCP на этом ПК', 'bad');
        banner('Сбер не ответил. Последний сохранённый снимок остаётся, свежие остатки не подтверждены.', 'bad');
      } else if (result.code === 'capture_failed' || result.code === 'ingest_rejected'
                 || result.code === 'history_unavailable') {
        const reason = result.reason || result.stage || result.source_error || 'без деталей';
        toast(`Сбер ответил, но данные не приняты: ${reason}`, 'bad');
        banner(`Сбер: полученные данные отклонены проверкой (${reason}). Снимок не заменён, подробности в data/cfo-review/sber-diagnostics.jsonl.`, 'bad');
      } else {
        toast(`Сбер не ответил: ${result.code || result.status || 'ошибка'}`, 'bad');
      }
      await loadCore();
      renderCurrentPage();
    } catch (error) {
      const code = error.payload?.code;
      toast(error.message, 'bad');
      if (code === 'auth_cooldown') {
        banner('Вход в Сбер временно приостановлен из-за частых попыток. Снимок остаётся честно помеченным.', 'warn');
      } else if (code === 'pin_delivery_not_private' || code === 'pin_delivery_missing') {
        banner('MCP не выдал приватный канал для PIN — нужен локальный вход через launcher Сбера.', 'bad');
      } else if (code && code !== 'pin_invalid') {
        banner(`Сбер не подтвердил вход (${code}). Детали — в настройках и журнале сервиса.`, 'warn');
      }
    } finally {
      button.disabled = false;
    }
  }

  async function checkPurchase(price) {
    try {
      const result = await api('/api/cfo/purchase', { method: 'POST', body: { price: String(price).replace(',', '.') } });
      const host = $('#purchase-result');
      host.hidden = false;
      const verdict = result.verdict || result.status;
      host.replaceChildren(
        el('b', {}, verdict ? String(verdict) : 'Расчёт готов'),
        el('p', { class: 'body-text' }, result.text || result.message || ''),
        result.after_purchase_low !== undefined
          ? el('p', { class: 'body-text' }, `После покупки: ${money(result.after_purchase_low)} – ${money(result.after_purchase_high)}`)
          : null);
    } catch (error) { toast(error.message, 'bad'); }
  }

  /* -------------------------------- events ------------------------------- */
  function bind() {
    $('#quick-add').addEventListener('click', () => openQuickAdd('expense'));
    $('#tab-add').addEventListener('click', () => openQuickAdd('expense'));
    $('#logout').addEventListener('click', async () => {
      try { await api('/auth/logout', { method: 'POST', body: {} }); } catch { /* ignore */ }
      location.replace('/');
    });
    $('#sync-btn').addEventListener('click', () => refreshBank());
    $('#bank-refresh').addEventListener('click', () => refreshBank());

    $('#pin-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const pin = new FormData(event.target).get('pin');
      if (!/^\d{5}$/.test(String(pin))) { toast('PIN — 5 цифр', 'bad'); return; }
      await refreshBank(String(pin));
    });

    $('#sync-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.target;
      try {
        await api('/api/cfo/runtime', { method: 'PUT', body: {
          bank_enabled: form.bank_enabled.checked,
          sync_minutes: Number(form.sync_minutes.value),
          daily_enabled: form.daily_enabled.checked,
          daily_hour: state.data.runtime?.settings?.daily_hour ?? 9,
          weekly_enabled: state.data.runtime?.settings?.weekly_enabled ?? false,
          weekly_hour: state.data.runtime?.settings?.weekly_hour ?? 9,
          alerts_enabled: form.alerts_enabled.checked,
          paid_classification_enabled: state.data.runtime?.settings?.paid_classification_enabled ?? true,
        } });
        toast('Настройки синхронизации сохранены', 'ok');
        state.data.runtime = await api('/api/cfo/runtime');
        renderSettings();
      } catch (error) { toast(error.message, 'bad'); }
    });

    $('#cash-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.target;
      const fact = (value) => (value === '' || value === null || value === undefined ? null
        : { low: String(value).replace(',', '.'), high: null, source: form.cash_source.value, updated_at: null });
      try {
        await saveProfile({
          cash: form.cash_low.value === '' ? null : {
            low: String(form.cash_low.value).replace(',', '.'),
            high: String(form.cash_high.value || form.cash_low.value).replace(',', '.'),
            source: form.cash_source.value,
          },
          reserve: fact(form.reserve.value),
          protected: fact(form.protected.value),
        });
      } catch (error) { toast(error.message, 'bad'); }
    });

    $('#obligations-complete').addEventListener('change', async (event) => {
      try { await saveProfile({ obligations_complete: event.target.checked }); }
      catch (error) { toast(error.message, 'bad'); }
    });

    $('#income-add').addEventListener('click', () => openProfileItem('income'));
    $('#obligation-add').addEventListener('click', () => openProfileItem('obligation'));
    $('#goal-new').addEventListener('click', () => openGoal(null));

    $('#purchase-form').addEventListener('submit', (event) => {
      event.preventDefault();
      checkPurchase(new FormData(event.target).get('price'));
    });

    $('#backup-btn').addEventListener('click', async () => {
      const button = $('#backup-btn');
      button.disabled = true;
      try {
        const result = await api('/api/cfo/backup', { method: 'POST', body: {} });
        $('#backup-note').textContent = `Готово: ${result.file} · проверена восстановлением · ${dateFull(result.created_at)}`;
        toast('Резервная копия создана', 'ok');
      } catch (error) { toast(error.message, 'bad'); }
      finally { button.disabled = false; }
    });

    // operations filters
    let searchTimer = null;
    $('#op-search').addEventListener('input', (event) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        state.filters.q = event.target.value.trim();
        await reloadOperations();
      }, 260);
    });
    $('#op-kind').addEventListener('click', async (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      state.filters.kind = button.dataset.kind;
      $$('#op-kind button').forEach((b) => b.classList.toggle('on', b === button));
      await reloadOperations();
    });
    $('#op-confirm').addEventListener('change', async (event) => {
      state.filters.confirmation = event.target.value;
      await reloadOperations();
    });
    $('#op-category').addEventListener('change', async (event) => {
      state.filters.category = event.target.value;
      await reloadOperations();
    });
    $('#op-period').addEventListener('change', async (event) => {
      state.filters.days = Number(event.target.value);
      await reloadOperations();
    });
    $('#op-more').addEventListener('click', async () => {
      await loadHistory(false);
      renderOperations(false);
    });
    $('#op-refresh').addEventListener('click', () => reloadOperations());
    $('#queue-toggle').addEventListener('click', (event) => {
      const list = $('#queue-list');
      const hidden = list.hidden;
      list.hidden = !hidden;
      state.queueOpen = hidden;
      event.target.textContent = hidden ? 'Свернуть' : 'Покниколайь';
    });

    // one AI section: Qwen dialog or a free local calculation
    $('#ai-mode').addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      state.coinMode = button.dataset.mode === 'local' ? 'local' : 'ai';
      $$('#ai-mode button').forEach((b) => b.classList.toggle('on', b === button));
      const input = $('#chat-input');
      input.placeholder = state.coinMode === 'local'
        ? 'Спросите: сколько можно потратить до дохода?'
        : 'Спросите Qwen о деньгах';
      $('#chat-note').textContent = state.coinMode === 'local'
        ? 'Локальный расчёт по вашим данным, бесплатно и без сети.'
        : (state.chat.status?.dispatch_allowed
            ? 'Qwen видит ваши локальные финансы через read-only инструменты. Ключ провайдера не попадает в браузер.'
            : 'Платный диалог на паузе. Переключитесь на «Расчёт» — он работает всегда.');
    });

    // chart period
    $('#chart-period').addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      loadOverview(Number(button.dataset.days));
    });

    // mobile "more" sheet
    $('#tab-more').addEventListener('click', () => {
      const sheet = $('#more-sheet');
      sheet.hidden = false;
      sheet.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    });
    $$('#more-sheet [data-close]').forEach((node) => node.addEventListener('click', () => {
      $('#more-sheet').hidden = true;
      $('#more-sheet').setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }));
    $$('#more-sheet .more-item').forEach((link) => link.addEventListener('click', () => {
      $('#more-sheet').hidden = true;
      $('#more-sheet').setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }));

    // interface effects: motion and glass are independent preferences
    const applyPrefs = () => {
      const motion = $('#motion-toggle')?.checked ?? true;
      const glass = $('#glass-toggle')?.checked ?? true;
      document.documentElement.dataset.motion = motion ? 'on' : 'off';
      document.documentElement.dataset.glass = glass ? 'on' : 'off';
      localStorage.setItem('jarvis.motion', motion ? 'on' : 'off');
      localStorage.setItem('jarvis.glass', glass ? 'on' : 'off');
      if (!motion) bgStop(); else if (bg.mode !== 'static') bgStart();
    };
    ['#motion-toggle', '#glass-toggle'].forEach((selector) => {
      $(selector)?.addEventListener('change', applyPrefs);
    });
    $('#bg-mode')?.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button || !button.dataset.bg) return;
      setBackgroundMode(button.dataset.bg);
    });

    // qwen chat
    $('#chat-form').addEventListener('submit', (event) => {
      event.preventDefault();
      if (state.chat.streaming) {
        state.chat.controller?.abort();
        return;
      }
      const input = $('#chat-input');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      input.style.height = 'auto';
      sendChat(text);
    });
    $('#chat-input').addEventListener('input', (event) => {
      event.target.style.height = 'auto';
      event.target.style.height = Math.min(event.target.scrollHeight, 160) + 'px';
    });
    $('#chat-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        $('#chat-form').requestSubmit();
      }
    });
    $('#chat-new').addEventListener('click', async () => {
      try {
        const created = await api('/api/ai/sessions', { method: 'POST', body: { title: 'Новый чат' } });
        state.chat.messages = [];
        await loadChatSessions();
        await openChat(created.session.id);
        $('#chat-input').focus();
      } catch (error) { toast(error.message, 'bad'); }
    });
    $('#chat-rename').addEventListener('click', () => {
      if (!state.chat.sessionId) return;
      const current = state.chat.session?.title || '';
      const form = el('form', { class: 'stack' },
        field('Название чата', input({ name: 'title', value: current, maxlength: 80, required: true })));
      openSheet('Переименовать', form, [{ label: 'Сохранить', kind: 'primary', run: async (close) => {
        if (!form.reportValidity()) return;
        const title = new FormData(form).get('title');
        try {
          await api(`/api/ai/sessions/${state.chat.sessionId}`, { method: 'PATCH', body: { title } });
          state.chat.session.title = title;
          $('#chat-title').textContent = title;
          await loadChatSessions();
          close();
        } catch (error) { toast(error.message, 'bad'); }
      } }]);
    });
    $('#chat-delete').addEventListener('click', () => {
      if (!state.chat.sessionId) return;
      const body = el('p', { class: 'body-text' }, 'Чат и все его сообщения будут удалены. Память и финансы останутся.');
      openSheet('Удалить чат?', body, [
        { label: 'Удалить', kind: 'primary', run: async (close) => {
          try {
            await api(`/api/ai/sessions/${state.chat.sessionId}`, { method: 'DELETE' });
            state.chat.sessionId = null;
            state.chat.messages = [];
            await loadChatSessions();
            if (state.chat.sessionId) await openChat(state.chat.sessionId);
            else renderChatThread();
            close();
            toast('Чат удалён', 'ok');
          } catch (error) { toast(error.message, 'bad'); }
        } },
      ]);
    });
    $('#chat-memory-btn').addEventListener('click', openMemory);
    $('#ai-journal-btn')?.addEventListener('click', openJournal);
    $('#pair-device')?.addEventListener('click', openPairSheet);
    $('#enable-passkey')?.addEventListener('click', registerPasskey);
    $('#budget-plan')?.addEventListener('click', openBudgetPlan);
    $('#queue-ai')?.addEventListener('click', runQueueAI);
    [['#autopilot-classify', 'classify'], ['#autopilot-budget', 'budget']].forEach(([selector, key]) => {
      $(selector)?.addEventListener('change', async (event) => {
        try {
          await api('/api/ai/autopilot', { method: 'PUT', body: { [key]: event.target.checked } });
          toast('Автопилот сохранён', 'ok');
        } catch (error) { toast(error.message, 'bad'); }
      });
    });
    $('#logout-all')?.addEventListener('click', () => {
      openSheet('Выйти на всех устройствах?', el('p', { class: 'body-text' },
        'Все доверенные устройства и Face ID будут отключены. Войти снова можно ключом владельца.'), [
        { label: 'Выйти везде', kind: 'primary', run: async (close) => {
          try {
            await api('/api/auth/logout-all', { method: 'POST', body: {} });
            close();
            location.replace('/');
          } catch (error) { toast(error.message, 'bad'); }
        } },
      ]);
    });
    $('#chat-drawer').addEventListener('click', openChatDrawer);
    $('#chat-search').addEventListener('input', (event) => {
      clearTimeout(state.chat.searchTimer);
      const value = event.target.value;
      state.chat.searchTimer = setTimeout(() => searchChats(value), 220);
    });

    window.addEventListener('hashchange', navigate);
    document.addEventListener('click', (event) => {
      if (event.target.dataset && event.target.dataset.close) closeSheet();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeSheet();
        closeChatDrawer();
        if (!$('#more-sheet').hidden) {
          $('#more-sheet').hidden = true;
          $('#more-sheet').setAttribute('aria-hidden', 'true');
          document.body.style.overflow = '';
        }
      }
    });
  }

  async function reloadOperations() {
    try {
      await loadHistory(true);
      renderOperations(true);
    } catch (error) { toast(error.message, 'bad'); }
  }

  /* --------------------------------- boot -------------------------------- */
  async function boot() {
    try {
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const savedMotion = localStorage.getItem('jarvis.motion');
      const savedGlass = localStorage.getItem('jarvis.glass');
      document.documentElement.dataset.motion = savedMotion === null
        ? (reduceMotion ? 'off' : 'on') : savedMotion;
      document.documentElement.dataset.glass = savedGlass === null ? 'on' : savedGlass;
      try {
        await csrfToken();
      } catch (error) {
        // A remembered device restores the session without the owner key.
        if (!(await restoreDeviceSession())) throw error;
        await csrfToken();
      }
      const failed = await loadCore();
      document.documentElement.dataset.webOnly = String(state.webOnly);
      $('#app').hidden = false;
      $('#boot').hidden = true;
      bind();
      initBackground();
      bindHeroGlow();
      bindViewportHeight();
      await navigate();
      renderPendingBadges();
      maybeAutopilot();
      const sync = state.data.runtime?.jobs?.sync || {};
      if (sync.status === 'auth_required') {
        const observed = state.data.cfo?.bank?.observed_at;
        banner(observed
          ? `Сбер: нужен повторный вход — показан снимок от ${dateShort(observed)}`
          : 'Сбер: нужен повторный вход. Введите PIN в настройках.', 'warn',
          { label: 'Настройки', run: () => { location.hash = '#/settings'; } });
      } else if (!state.data.cfo?.bank) {
        banner('Банковский снимок отсутствует. Подключите Сбер в настройках, чтобы видеть реальные остатки.', 'warn',
          { label: 'Настройки', run: () => { location.hash = '#/settings'; } });
      } else if (failed.length) {
        banner(`Часть разделов недоступна: ${failed.join(', ')}. Остальное работает на сохранённых данных.`, 'info');
      }
    } catch (error) {
      $('#boot').hidden = true;
      banner(`Не удалось загрузить приложение: ${error.message}`, 'bad');
      document.body.append(el('p', { class: 'empty' }, 'Обновите страницу или войдите заново.'));
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
