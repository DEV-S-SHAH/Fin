/* FinGraph Company Deep Dive — Unified Studio + Landing Design
 *
 * Combines:
 *   - Studio app shell (topbar, workspace, splitter, tabs, side panel)
 *   - Landing page visual language (gradient bars, cards, glow, animations)
 *   - Live data sync via polling + SSE-ready architecture
 */

import {
  fetchCompanies, fetchStats, fetchGraph,
  fetchEntities, askJson,
} from "/static/api.js";
import { GraphView } from "/static/graph.js";

import { set, setCollection, state, subscribe } from "/static/store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor, stampLogos,
} from "/static/util.js";

// Chart.js for mini charts - loaded globally from vendor/chart.min.js
const Chart = window.Chart;

// ===== Configuration =====
const API_BASE = '/api/company';
const POLL_INTERVAL = 30000; // 30 seconds
const CHART_COLORS = {
  up: '#00D4AA',
  down: '#FF3C00',
  grid: 'rgba(255, 255, 255, 0.04)',
  text: 'rgba(255, 255, 255, 0.5)',
  tooltipBg: 'rgba(15, 15, 17, 0.95)',
  tooltipBorder: 'rgba(255, 255, 255, 0.1)',
  sma20: '#FF3C00',
  sma50: '#FFB800',
  sma200: '#00D4AA',
  bb: 'rgba(255, 60, 0, 0.2)',
  volume: 'rgba(255, 255, 255, 0.15)'
};

// ===== State =====
let appState = {
  ticker: '',
  data: null,
  currentTimeframe: '1D',
  chartType: 'candlestick',
  activeIndicators: { sma20: true, sma50: true, bb: false, volume: true },
  activeFundTab: 'valuation',
  activeDetailTab: 'overview',
  charts: { mini: null, main: null, kg: null },
  pollTimer: null,
  graph: null,
  companies: [],
  selectedEntity: null,
};

// ===== DOM Elements =====
const els = {};

// ===== Utility Functions =====
const $q = (sel, ctx = document) => ctx.querySelector(sel);
const $qa = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

const formatNumber = (num, decimals = 2) => {
  if (num === null || num === undefined || isNaN(num)) return '—';
  const abs = Math.abs(num);
  if (abs >= 1e12) return (num / 1e12).toFixed(decimals) + 'T';
  if (abs >= 1e9) return (num / 1e9).toFixed(decimals) + 'B';
  if (abs >= 1e6) return (num / 1e6).toFixed(decimals) + 'M';
  if (abs >= 1e3) return (num / 1e3).toFixed(decimals) + 'K';
  return num.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
};

const formatPct = (num, decimals = 2) => {
  if (num === null || num === undefined || isNaN(num)) return '—';
  const sign = num >= 0 ? '+' : '';
  return `${sign}${num.toFixed(decimals)}%`;
};

const formatCurrency = (num, currency = 'USD') => {
  if (num === null || num === undefined || isNaN(num)) return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(num);
};

const formatDate = (timestamp) => {
  if (!timestamp) return '—';
  const date = timestamp > 1e10 ? new Date(timestamp) : new Date(timestamp * 1000);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
};

const showToast = (message, type = 'info') => {
  const container = $q('#toast-container') || $q('#toasts');
  const toastEl = document.createElement('div');
  toastEl.className = `toast toast--${type}`;
  toastEl.innerHTML = `
    <span class="toast__message">${message}</span>
    <button class="toast__close" aria-label="Dismiss">&times;</button>
  `;
  toastEl.querySelector('.toast__close').addEventListener('click', () => toastEl.remove());
  container.appendChild(toastEl);
  setTimeout(() => toastEl.remove(), 5000);
};

const setLoading = (show) => {
  const overlay = $q('#loading-overlay');
  if (overlay) overlay.hidden = !show;
};

// ===== API Functions =====
async function fetchCompanyData(ticker) {
  try {
    const res = await fetch(`${API_BASE}/${ticker.toUpperCase()}`);
    if (!res.ok) {
      if (res.status === 404) throw new Error('Company not found');
      throw new Error(`Failed to fetch: ${res.statusText}`);
    }
    return await res.json();
  } catch (err) {
    console.error('Company data fetch error:', err);
    throw err;
  }
}

// ===== Chart Data Processing =====
function processChartData(chartData, timeframe) {
  if (!chartData) return { labels: [], prices: [], volumes: [], timestamps: [], raw: [] };

  const timestamps = chartData.timestamp || [];
  const quotes = chartData.indicators?.quote?.[0] || {};
  const closes = quotes.close || [];
  const opens = quotes.open || [];
  const highs = quotes.high || [];
  const lows = quotes.low || [];
  const volumes = quotes.volume || [];

  const valid = [];
  for (let i = 0; i < timestamps.length; i++) {
    if (closes[i] !== null && opens[i] !== null && highs[i] !== null && lows[i] !== null) {
      valid.push({
        t: timestamps[i] * 1000,
        o: opens[i],
        h: highs[i],
        l: lows[i],
        c: closes[i],
        v: volumes[i] || 0
      });
    }
  }

  return {
    labels: valid.map(d => new Date(d.t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })),
    prices: valid.map(d => d.c),
    opens: valid.map(d => d.o),
    highs: valid.map(d => d.h),
    lows: valid.map(d => d.l),
    volumes: valid.map(d => d.v),
    timestamps: valid.map(d => d.t),
    raw: valid
  };
}

function getChartDataForTimeframe(timeframe) {
  const { data } = appState;
  if (!data) return null;

  switch (timeframe) {
    case '1D':
    case '1W':
      return processChartData(data.chart_1mo, timeframe);
    case '1M':
    case '3M':
      return processChartData(data.chart_3mo, timeframe);
    case '1Y':
    case 'ALL':
      return processChartData(data.chart_1y, timeframe);
    default:
      return processChartData(data.chart_1mo, timeframe);
  }
}

function calculateSMA(data, period) {
  const result = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) {
      result.push(null);
    } else {
      const sum = data.slice(i - period + 1, i + 1).reduce((a, b) => a + b, 0);
      result.push(sum / period);
    }
  }
  return result;
}

function calculateBollingerBands(data, period = 20, stdDev = 2) {
  const upper = [], middle = [], lower = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) {
      upper.push(null); middle.push(null); lower.push(null);
    } else {
      const slice = data.slice(i - period + 1, i + 1);
      const avg = slice.reduce((a, b) => a + b, 0) / period;
      const std = Math.sqrt(slice.reduce((a, b) => a + Math.pow(b - avg, 2), 0) / period);
      middle.push(avg);
      upper.push(avg + stdDev * std);
      lower.push(avg - stdDev * std);
    }
  }
  return { upper, middle, lower };
}

// ===== Rendering Functions =====
function renderCompanyIdentity(data) {
  const { quote, fundamentals } = data;
  if (!quote) return;

  $q('#company-ticker').textContent = quote.ticker;
  $q('#page-title').textContent = `${quote.ticker} - FinGraph`;
  $q('#company-name').textContent = fundamentals.description?.split('.')[0] || `${quote.ticker} Inc.`;
  $q('#company-exchange').textContent = quote.exchange || 'N/A';
  $q('#company-sector').textContent = fundamentals.sector || 'N/A';
  $q('#company-currency').textContent = quote.currency || 'USD';

  $q('#ask-btn').dataset.ticker = quote.ticker;
  $q('#ask-btn').href = `/app?ticker=${quote.ticker}`;

  // Update ticker select
  const select = $q('#ticker-select');
  if (select) select.value = quote.ticker;
}

function renderLivePrice(data) {
  const { quote, technicals, fundamentals } = data;
  if (!quote) return;

  $q('#current-price').textContent = formatCurrency(quote.price, quote.currency);

  if (quote.change !== null && quote.pct !== null) {
    const isPositive = quote.change >= 0;
    $q('#change-value').textContent = `${isPositive ? '+' : ''}${formatNumber(quote.change)} (${formatPct(quote.pct)})`;
    $q('#change-value').className = `change-value ${isPositive ? 'positive' : 'negative'}`;
  } else {
    $q('#change-value').textContent = '—';
    $q('#change-value').className = 'change-value';
  }

  const marketStates = {
    'REGULAR': 'Market Open',
    'PRE': 'Pre-Market',
    'POST': 'After Hours',
    'CLOSED': 'Market Closed'
  };
  $q('#market-state').textContent = marketStates[quote.market_state] || 'Market Closed';

  // Price stats
  const statsContainer = $q('#price-stats');
  const stats = [
    { label: 'Open', value: quote.price },
    { label: 'High', value: technicals?.bb_upper },
    { label: 'Low', value: technicals?.bb_lower },
    { label: 'Volume', value: fundamentals?.avg_volume },
    { label: 'Market Cap', value: fundamentals?.market_cap_raw },
    { label: 'P/E Ratio', value: fundamentals?.pe_ratio }
  ];

  statsContainer.innerHTML = stats.map(s => `
    <div class="price-stat">
      <span class="price-stat__label">${s.label}</span>
      <span class="price-stat__value">${s.value ? (typeof s.value === 'string' ? s.value : formatNumber(s.value)) : '—'}</span>
    </div>
  `).join('');
}

function renderMiniChart() {
  const chartData = getChartDataForTimeframe(appState.currentTimeframe);
  if (!chartData || chartData.prices.length === 0) return;

  const canvas = $q('#mini-chart');
  const ctx = canvas.getContext('2d');
  const width = canvas.width = canvas.offsetWidth * devicePixelRatio;
  const height = canvas.height = canvas.offsetHeight * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);

  const cssWidth = canvas.offsetWidth;
  const cssHeight = canvas.offsetHeight;
  const prices = chartData.prices;
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const range = maxPrice - minPrice || 1;

  ctx.clearRect(0, 0, cssWidth, cssHeight);

  // Gradient area
  const gradient = ctx.createLinearGradient(0, 0, 0, cssHeight);
  gradient.addColorStop(0, 'rgba(255, 60, 0, 0.3)');
  gradient.addColorStop(1, 'rgba(255, 60, 0, 0)');

  ctx.beginPath();
  ctx.moveTo(0, cssHeight);
  prices.forEach((price, i) => {
    const x = (i / (prices.length - 1)) * cssWidth;
    const y = cssHeight - ((price - minPrice) / range) * cssHeight;
    if (i === 0) ctx.lineTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(cssWidth, cssHeight);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Line
  ctx.beginPath();
  prices.forEach((price, i) => {
    const x = (i / (prices.length - 1)) * cssWidth;
    const y = cssHeight - ((price - minPrice) / range) * cssHeight;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = CHART_COLORS.up;
  ctx.lineWidth = 2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.stroke();

  appState.charts.mini = { ctx, data: chartData, cssWidth, cssHeight, minPrice, maxPrice };
}

function renderMainChart() {
  // Not used in this unified page - we only show mini chart in hero
}

function renderTechnicals(data) {
  const { technicals } = data;
  if (!technicals) return;

  const grid = $q('#technicals-grid');
  const badge = $q('#technicals-signal');

  let bullish = 0, bearish = 0;
  const checks = [
    { val: technicals.price_vs_sma20, label: 'vs SMA 20', key: 'price_vs_sma20' },
    { val: technicals.price_vs_sma50, label: 'vs SMA 50', key: 'price_vs_sma50' },
    { val: technicals.price_vs_sma200, label: 'vs SMA 200', key: 'price_vs_sma200' },
    { val: technicals.rsi_14, label: 'RSI (14)', key: 'rsi_14', invert: true },
    { val: technicals.macd_histogram, label: 'MACD Hist', key: 'macd_histogram' }
  ];

  checks.forEach(c => {
    if (c.val !== null && c.val !== undefined) {
      if (c.invert) {
        if (c.val < 30) bullish++;
        else if (c.val > 70) bearish++;
      } else {
        if (c.val > 0) bullish++;
        else if (c.val < 0) bearish++;
      }
    }
  });

  let signal = 'Neutral', signalClass = 'card__badge--neutral';
  if (bullish > bearish + 1) { signal = 'Bullish'; signalClass = 'card__badge--bullish'; }
  else if (bearish > bullish + 1) { signal = 'Bearish'; signalClass = 'card__badge--bearish'; }
  badge.textContent = signal;
  badge.className = `card__badge ${signalClass}`;

  const items = [
    { label: 'RSI (14)', value: technicals.rsi_14, fmt: v => v?.toFixed(1), class: v => v > 70 ? 'negative' : v < 30 ? 'positive' : '' },
    { label: 'MACD', value: technicals.macd, fmt: v => v?.toFixed(4) },
    { label: 'Signal', value: technicals.macd_signal, fmt: v => v?.toFixed(4) },
    { label: 'Histogram', value: technicals.macd_histogram, fmt: v => v?.toFixed(4), class: v => v > 0 ? 'positive' : 'negative' },
    { label: 'SMA 20', value: technicals.sma_20, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma20 !== null ? `${technicals.price_vs_sma20 > 0 ? '+' : ''}${technicals.price_vs_sma20.toFixed(2)}%` : null, subClass: technicals.price_vs_sma20 > 0 ? 'positive' : 'negative' },
    { label: 'SMA 50', value: technicals.sma_50, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma50 !== null ? `${technicals.price_vs_sma50 > 0 ? '+' : ''}${technicals.price_vs_sma50.toFixed(2)}%` : null, subClass: technicals.price_vs_sma50 > 0 ? 'positive' : 'negative' },
    { label: 'SMA 200', value: technicals.sma_200, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma200 !== null ? `${technicals.price_vs_sma200 > 0 ? '+' : ''}${technicals.price_vs_sma200.toFixed(2)}%` : null, subClass: technicals.price_vs_sma200 > 0 ? 'positive' : 'negative' },
    { label: 'BB Upper', value: technicals.bb_upper, fmt: v => formatCurrency(v) },
    { label: 'BB Lower', value: technicals.bb_lower, fmt: v => formatCurrency(v) },
    { label: 'ATR (14)', value: technicals.atr_14, fmt: v => formatCurrency(v) }
  ];

  grid.innerHTML = items.map(item => {
    const val = item.value;
    const formatted = val !== null && val !== undefined ? item.fmt(val) : '—';
    const valClass = item.class ? item.class(val) : '';
    const subHtml = item.sub ? `<span class="tech-item__sub ${item.subClass || ''}">${item.sub}</span>` : '';
    return `
      <div class="tech-item">
        <span class="tech-item__label">${item.label}</span>
        <span class="tech-item__value ${valClass}">${formatted}</span>
        ${subHtml}
      </div>
    `;
  }).join('');
}

function renderFundamentals(data) {
  const { fundamentals } = data;
  if (!fundamentals) return;

  const panels = $q('#fundamentals-panels');

  const tabs = {
    valuation: [
      { label: 'Market Cap', key: 'market_cap' },
      { label: 'P/E (TTM)', key: 'pe_ratio', fmt: v => v?.toFixed(2) },
      { label: 'Forward P/E', key: 'forward_pe', fmt: v => v?.toFixed(2) },
      { label: 'PEG Ratio', key: 'peg_ratio', fmt: v => v?.toFixed(2) },
      { label: 'P/B Ratio', key: 'price_to_book', fmt: v => v?.toFixed(2) },
      { label: 'Enterprise Value', key: 'enterprise_value' },
      { label: '52W High', key: '52wk_high', fmt: v => formatCurrency(v) },
      { label: '52W Low', key: '52wk_low', fmt: v => formatCurrency(v) },
      { label: '50D Avg', key: '50d_avg', fmt: v => formatCurrency(v) },
      { label: '200D Avg', key: '200d_avg', fmt: v => formatCurrency(v) },
      { label: 'Beta', key: 'beta', fmt: v => v?.toFixed(2) },
      { label: 'Shares Out.', key: 'shares_outstanding' },
      { label: 'Float', key: 'float_shares' },
      { label: 'Short %', key: 'short_percent', fmt: v => formatPct(v * 100) }
    ],
    profitability: [
      { label: 'Revenue', key: 'revenue' },
      { label: 'Gross Profit', key: 'gross_profit' },
      { label: 'EBITDA', key: 'ebitda' },
      { label: 'Net Income', key: 'net_income' },
      { label: 'Diluted EPS', key: 'diluted_eps', fmt: v => formatCurrency(v) },
      { label: 'Forward EPS', key: 'forward_eps', fmt: v => formatCurrency(v) },
      { label: 'Profit Margin', key: 'profit_margin', fmt: v => formatPct(v * 100) },
      { label: 'Operating Margin', key: 'operating_margin', fmt: v => formatPct(v * 100) },
      { label: 'ROE', key: 'return_on_equity', fmt: v => formatPct(v * 100) },
      { label: 'ROA', key: 'return_on_assets', fmt: v => formatPct(v * 100) },
      { label: 'Rev/Share', key: 'revenue_per_share', fmt: v => formatCurrency(v) }
    ],
    'financial-health': [
      { label: 'Total Cash', key: 'total_cash' },
      { label: 'Total Debt', key: 'total_debt' },
      { label: 'D/E Ratio', key: 'debt_to_equity', fmt: v => v?.toFixed(2) },
      { label: 'Current Ratio', key: 'current_ratio', fmt: v => v?.toFixed(2) },
      { label: 'Quick Ratio', key: 'quick_ratio', fmt: v => v?.toFixed(2) },
      { label: 'Free Cash Flow', key: 'free_cash_flow' },
      { label: 'Operating CF', key: 'operating_cash_flow' },
      { label: 'Avg Volume', key: 'avg_volume', fmt: v => formatNumber(v) },
      { label: 'Avg Vol (10d)', key: 'avg_volume_10d', fmt: v => formatNumber(v) }
    ],
    dividends: [
      { label: 'Dividend Yield', key: 'dividend_yield', fmt: v => formatPct(v * 100) },
      { label: 'Dividend Rate', key: 'dividend_rate', fmt: v => formatCurrency(v) },
      { label: 'Ex-Dividend Date', key: 'ex_dividend_date', fmt: v => formatDate(v) },
      { label: 'Payout Ratio', key: 'payout_ratio', fmt: v => formatPct(v * 100) }
    ]
  };

  panels.innerHTML = Object.entries(tabs).map(([tabKey, fields]) => `
    <div class="fund-panel ${tabKey === appState.activeFundTab ? 'active' : ''}" data-tab="${tabKey}" role="tabpanel">
      <div class="fund-grid">
        ${fields.map(f => {
          const val = fundamentals[f.key];
          const formatted = val !== null && val !== undefined && val !== '' ? (f.fmt ? f.fmt(val) : val) : '—';
          const highlight = ['market_cap', 'revenue', 'net_income', 'free_cash_flow'].includes(f.key) ? ' fund-item__value--highlight' : '';
          return `
            <div class="fund-item">
              <span class="fund-item__label">${f.label}</span>
              <span class="fund-item__value${highlight}">${formatted}</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `).join('');

  $qa('.fund-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $qa('.fund-tab').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      $qa('.fund-panel').forEach(p => p.classList.remove('active'));
      $q(`[data-tab="${btn.dataset.tab}"]`).classList.add('active');
      appState.activeFundTab = btn.dataset.tab;
    });
  });
}

function renderNews(data) {
  const { news } = data;
  const list = $q('#news-list');

  if (!news || news.length === 0) {
    list.innerHTML = '<p class="news-empty" style="text-align:center;color:var(--color-text-muted);padding:32px;">No recent news available</p>';
    return;
  }

  list.innerHTML = news.map(item => `
    <a href="${item.link}" target="_blank" rel="noopener noreferrer" class="news-item" aria-label="${item.title}">
      ${item.thumbnail ? `<img class="news-item__thumb" src="${item.thumbnail}" alt="" loading="lazy">` : '<div class="news-item__thumb news-item__thumb--placeholder">📰</div>'}
      <div class="news-item__content">
        <h3 class="news-item__title">${item.title}</h3>
        <div class="news-item__meta">
          <span class="news-item__publisher">${item.publisher || 'Unknown'}</span>
          <span class="news-item__time">${formatTimeAgo(item.provider_publish_time)}</span>
        </div>
      </div>
    </a>
  `).join('');
}

function formatTimeAgo(timestamp) {
  if (!timestamp) return '—';
  const date = timestamp > 1e10 ? new Date(timestamp) : new Date(timestamp * 1000);
  const diff = Date.now() - date.getTime();
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days < 7) return `${days}d ago`;
  return formatDate(timestamp);
}

function renderKnowledgeGraph(data) {
  const { graph_info } = data;
  const canvas = $q('#kg-canvas');
  const empty = $q('#kg-empty');
  const legend = $q('#kg-legend');

  if (!graph_info?.in_graph) {
    canvas.hidden = true;
    empty.hidden = false;
    legend.hidden = true;
    return;
  }

  canvas.hidden = false;
  empty.hidden = true;
  legend.hidden = false;

  const ctx = canvas.getContext('2d');
  const width = canvas.width = canvas.offsetWidth * devicePixelRatio;
  const height = canvas.height = canvas.offsetHeight * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const cssWidth = canvas.offsetWidth;
  const cssHeight = canvas.offsetHeight;

  const nodes = [
    { id: appState.ticker, label: appState.ticker, type: 'company', x: cssWidth / 2, y: cssHeight / 2, fx: cssWidth / 2, fy: cssHeight / 2 },
    { id: 'sector', label: data.fundamentals.sector || 'Technology', type: 'sector', x: cssWidth / 2 + 150, y: cssHeight / 2 - 80 },
    { id: 'industry', label: data.fundamentals.industry || 'Software', type: 'industry', x: cssWidth / 2 - 150, y: cssHeight / 2 - 80 },
    { id: 'filings', label: `${graph_info.filings?.length || 0} Filings`, type: 'filings', x: cssWidth / 2 + 100, y: cssHeight / 2 + 100 },
    { id: 'peers', label: 'Peers', type: 'peers', x: cssWidth / 2 - 100, y: cssHeight / 2 + 100 }
  ];

  const links = [
    { source: appState.ticker, target: 'sector' },
    { source: appState.ticker, target: 'industry' },
    { source: appState.ticker, target: 'filings' },
    { source: appState.ticker, target: 'peers' }
  ];

  const typeColors = {
    company: '#FF3C00',
    sector: '#FFB800',
    industry: '#00D4AA',
    filings: '#6366F1',
    peers: '#EC4899'
  };

  // Draw links
  ctx.strokeStyle = 'rgba(255,255,255,0.15)';
  ctx.lineWidth = 1.5;
  links.forEach(link => {
    const s = nodes.find(n => n.id === link.source);
    const t = nodes.find(n => n.id === link.target);
    if (s && t) {
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      ctx.stroke();
    }
  });

  // Draw nodes
  nodes.forEach(node => {
    const color = typeColors[node.type] || '#888';
    const radius = node.type === 'company' ? 28 : 22;

    const gradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, radius + 10);
    gradient.addColorStop(0, color + '40');
    gradient.addColorStop(1, 'transparent');
    ctx.beginPath();
    ctx.arc(node.x, node.y, radius + 10, 0, Math.PI * 2);
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();

    ctx.strokeStyle = '#030303';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.font = '500 12px var(--font-sans)';
    ctx.fillStyle = '#fff';
    ctx.textAlign = 'center';
    ctx.fillText(node.label, node.x, node.y + radius + 18);
  });

  legend.innerHTML = Object.entries(typeColors).map(([type, color]) => `
    <span class="kg-legend-item">
      <span class="kg-legend-color" style="background: ${color}"></span>
      ${type.charAt(0).toUpperCase() + type.slice(1)}
    </span>
  `).join('');
}

function renderFilings(data) {
  const { graph_info } = data;
  const tbody = $q('#filings-body');
  const countEl = $q('#filings-count');
  const empty = $q('#filings-empty');
  const table = $q('#filings-table');

  if (!graph_info?.in_graph || !graph_info.filings?.length) {
    table.hidden = true;
    empty.hidden = false;
    if (countEl) countEl.textContent = '0 filings';
    return;
  }

  table.hidden = false;
  empty.hidden = true;
  if (countEl) countEl.textContent = `${graph_info.filings.length} filings`;

  tbody.innerHTML = graph_info.filings.map(f => `
    <tr>
      <td><span class="form-type">${f.form}</span></td>
      <td>${f.fiscal_year}</td>
      <td>${f.period === 'FY' ? 'Full Year' : 'Q' + f.period}</td>
      <td>${formatDate(f.period_end)}</td>
    </tr>
  `).join('');
}

function renderOverview(data) {
  const { fundamentals, quote } = data;
  const content = $q('#overview-content');

  if (!fundamentals) {
    content.innerHTML = '<p style="color:var(--color-text-muted);text-align:center;padding:48px;">No overview data available</p>';
    return;
  }

  content.innerHTML = `
    <div class="description-content">
      ${fundamentals.description ? `<p>${fundamentals.description}</p>` : '<p style="color:var(--color-text-muted)">No description available.</p>'}
    </div>
    <div class="description-meta">
      <div class="meta-item"><span class="meta-item__label">Sector</span><span class="meta-item__value">${fundamentals.sector || '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Industry</span><span class="meta-item__value">${fundamentals.industry || '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Employees</span><span class="meta-item__value">${fundamentals.employees ? fundamentals.employees.toLocaleString() : '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Country</span><span class="meta-item__value">${fundamentals.country || '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Website</span><span class="meta-item__value">${fundamentals.website ? `<a href="${fundamentals.website}" target="_blank" rel="noopener" style="color:var(--color-primary)">${fundamentals.website}</a>` : '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Exchange</span><span class="meta-item__value">${quote?.exchange || '—'}</span></div>
      <div class="meta-item"><span class="meta-item__label">Currency</span><span class="meta-item__value">${quote?.currency || 'USD'}</span></div>
    </div>
  `;
}

function renderSegments(data) {
  // Placeholder - segment data not in current API
  const body = $q('#segments-body');
  const empty = $q('#segments-empty');
  const table = $q('#segments-table');

  table.hidden = true;
  empty.hidden = false;
  body.innerHTML = '';
}

function renderPeers(data) {
  const grid = $q('#peers-grid');
  // Use companies from graph as peers
  const peers = appState.companies.filter(c => c.ticker !== appState.ticker).slice(0, 6);

  if (!peers.length) {
    grid.innerHTML = '<p style="color:var(--color-text-muted);text-align:center;padding:48px;">No peer data available</p>';
    return;
  }

  grid.innerHTML = peers.map(peer => `
    <div class="peer-card">
      <div class="peer-card__ticker">${peer.ticker}</div>
      <div class="peer-card__name">${peer.name || peer.legal_name || ''}</div>
      <div class="peer-card__meta">
        <span>${peer.filings || 0} filings</span>
        <span>${peer.latest?.form || ''} ${peer.latest?.fiscal_year || ''}</span>
      </div>
    </div>
  `).join('');
}

// ===== Graph (Studio style) =====
class CompanyGraphView {
  constructor() {
    this.graph = new GraphView($q('#graph-canvas'), {
      onSelect: (node) => {
        set({ selected: node?.id || null });
        this.#renderEntities();
      },
      tooltip: $q('#graph-tooltip'),
      tooltipHost: $q('#graph-panel'),
      inspector: $q('#graph-inspector'),
      sidePanel: $q('#graph-side-panel'),
    });
  }

  async loadGraph(ticker) {
    set({ graphLoading: true });
    try {
      const payload = await fetchGraph({ seed: ticker, hops: 2, limit: 150 });
      set({ graph: payload, selected: null });
      const counts = this.graph.setData(payload, { fresh: true });
      $q('#graph-count').textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
    } catch (error) {
      toast(`graph query failed: ${error.message}`, 'bad');
    } finally {
      set({ graphLoading: false });
    }
  }
}

// ===== Event Handlers =====
function handleTimeframeChange(btn) {
  const tf = btn.dataset.tf;
  if (tf === appState.currentTimeframe) return;

  $qa('.timeframe-btn').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
  btn.classList.add('active');
  btn.setAttribute('aria-pressed', 'true');
  appState.currentTimeframe = tf;
  renderMiniChart();
}

function handleDetailTabChange(tab) {
  const name = tab.dataset.tab;
  if (name === appState.activeDetailTab) return;

  $qa('.tab[data-tab]').forEach(t => {
    const on = t.dataset.tab === name;
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    t.classList.toggle('is-on', on);
  });
  $qa('.tabpanel').forEach(p => {
    p.hidden = p.id !== `tab-${name}`;
  });
  appState.activeDetailTab = name;

  // Render tab content if needed
  if (name === 'segments' && appState.data) renderSegments(appState.data);
  if (name === 'peers' && appState.data) renderPeers(appState.data);
}

async function handleTickerChange(select) {
  const ticker = select.value;
  if (!ticker) return;
  await loadCompanyData(ticker);
}

// ===== Live Data Sync =====
let pollTimer = null;

function startPolling(ticker) {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      // Only refresh quote/price data (lightweight)
      const res = await fetch(`${API_BASE}/${ticker}`);
      if (res.ok) {
        const data = await res.json();
        // Update only live elements
        if (data.quote) updateLivePrice(data.quote);
        if (data.technicals) updateTechnicals(data.technicals);
      }
    } catch (err) {
      console.warn('Polling failed:', err);
    }
  }, POLL_INTERVAL);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function updateLivePrice(quote) {
  $q('#current-price').textContent = formatCurrency(quote.price, quote.currency);
  if (quote.change !== null && quote.pct !== null) {
    const isPositive = quote.change >= 0;
    $q('#change-value').textContent = `${isPositive ? '+' : ''}${formatNumber(quote.change)} (${formatPct(quote.pct)})`;
    $q('#change-value').className = `change-value ${isPositive ? 'positive' : 'negative'}`;
  }
  const marketStates = { 'REGULAR': 'Market Open', 'PRE': 'Pre-Market', 'POST': 'After Hours', 'CLOSED': 'Market Closed' };
  $q('#market-state').textContent = marketStates[quote.market_state] || 'Market Closed';
}

function updateTechnicals(technicals) {
  // Update technicals grid values
  $qa('.tech-item').forEach(item => {
    const label = item.querySelector('.tech-item__label')?.textContent;
    const valueEl = item.querySelector('.tech-item__value');
    const subEl = item.querySelector('.tech-item__sub');
    if (!label || !valueEl) return;

    const keyMap = {
      'RSI (14)': 'rsi_14',
      'MACD': 'macd',
      'Signal': 'macd_signal',
      'Histogram': 'macd_histogram',
      'SMA 20': 'sma_20',
      'SMA 50': 'sma_50',
      'SMA 200': 'sma_200',
      'BB Upper': 'bb_upper',
      'BB Lower': 'bb_lower',
      'ATR (14)': 'atr_14'
    };
    const key = keyMap[label.trim()];
    if (key && technicals[key] !== undefined) {
      const val = technicals[key];
      if (label.includes('SMA') || label.includes('BB') || label.includes('ATR')) {
        valueEl.textContent = formatCurrency(val);
      } else if (label === 'RSI (14)') {
        valueEl.textContent = val?.toFixed(1);
      } else {
        valueEl.textContent = val?.toFixed(4);
      }
    }
  });
}

// ===== Main Load Function =====
async function loadCompanyData(ticker) {
  if (!ticker || !ticker.match(/^[A-Za-z]+$/)) {
    showToast('Invalid company ticker', 'error');
    return;
  }

  appState.ticker = ticker.toUpperCase();
  setLoading(true);

  try {
    appState.data = await fetchCompanyData(appState.ticker);
    if (!appState.data) throw new Error('No data returned');

    // Render all sections
    renderCompanyIdentity(appState.data);
    renderLivePrice(appState.data);
    renderMiniChart();
    renderTechnicals(appState.data);
    renderFundamentals(appState.data);
    renderNews(appState.data);
    renderKnowledgeGraph(appState.data);
    renderFilings(appState.data);
    renderOverview(appState.data);

    // Load graph
    await appState.graph.loadGraph(appState.ticker);

    // Start live polling
    startPolling(appState.ticker);

  } catch (err) {
    console.error('Failed to load company data:', err);
    showToast(`Failed to load ${appState.ticker}: ${err.message}`, 'error');
  } finally {
    setLoading(false);
  }
}

// ===== Initialization =====
async function init() {
  stampLogos();
  appState.graph = new CompanyGraphView();

  // Wire topbar actions
  $qa('.timeframe-btn').forEach(btn => {
    btn.addEventListener('click', () => handleTimeframeChange(btn));
  });

  $qa('.tab[data-tab]').forEach(tab => {
    tab.addEventListener('click', () => handleDetailTabChange(tab));
  });

  $q('#ticker-select')?.addEventListener('change', (e) => handleTickerChange(e.target));

  // Mobile nav
  $qa('.mobile-nav [role="tab"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      $qa('.mobile-nav [role="tab"]').forEach(b => b.setAttribute('aria-selected', b === btn ? 'true' : 'false'));
      document.body.dataset.view = view;
    });
  });

  // Theme toggle
  $q('#theme-btn')?.addEventListener('click', () => {
    const html = document.documentElement;
    const newTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
    renderMiniChart();
    renderKnowledgeGraph(appState.data);
  });

  // Watchlist button
  $q('#watchlist-btn')?.addEventListener('click', () => {
    showToast(`${appState.ticker} added to watchlist`, 'success');
  });

  // Load companies for selector
  try {
    const companiesData = await fetchCompanies();
    appState.companies = companiesData.companies || [];
    const select = $q('#ticker-select');
    if (select) {
      select.innerHTML = '<option value="">Select company…</option>' +
        appState.companies.map(c => `<option value="${c.ticker}">${c.ticker} — ${c.legal_name || c.name || ''}</option>`).join('');
    }

    // Load stats for topbar
    const stats = await fetchStats();
    set({ stats, rag: stats });
    $q('#stat-nodes').textContent = fmtNumber(stats.nodes ?? 0);
    $q('#stat-edges').textContent = fmtNumber(stats.edges ?? 0);
    $q('#stat-issuers').textContent = fmtNumber(appState.companies.length);
    const chip = $q('#schema-chip');
    if (stats.schema) {
      chip.hidden = false;
      chip.textContent = stats.schema;
      chip.title = `LadybugDB schema: ${stats.schema}`;
    }
  } catch (err) {
    console.warn('Failed to load initial data:', err);
  }

  // Get ticker from URL
  const path = window.location.pathname;
  const match = path.match(/\/company\/([A-Za-z]+)/);
  if (match) {
    await loadCompanyData(match[1].toUpperCase());
  }

  // Handle resize
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (appState.charts.mini) renderMiniChart();
      if (appState.charts.kg) renderKnowledgeGraph(appState.data);
    }, 150);
  });

  // Cleanup on unload
  window.addEventListener('beforeunload', stopPolling);

  // Handle browser back/forward
  window.addEventListener('popstate', async () => {
    const path = window.location.pathname;
    const match = path.match(/\/company\/([A-Za-z]+)/);
    if (match) {
      appState = { ...appState, data: null, charts: { mini: null, main: null, kg: null } };
      await loadCompanyData(match[1].toUpperCase());
    }
  });
}

// Start
document.addEventListener('DOMContentLoaded', init);