/* FinGraph Company Deep Dive — Technicals-Focused Page
 *
 * Pure company technicals & fundamentals from Yahoo Finance.
 * No GraphRAG, no Ask panel, no Knowledge Graph.
 * Live data sync via 30s polling.
 */

import {
  fetchCompanies, fetchStats,
} from "/static/api.js";

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
  charts: { mini: null },
  pollTimer: null,
  companies: [],
};

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

function renderTechnicals(data) {
  const { technicals, quote } = data;
  if (!technicals) return;

  const grid = $q('#technicals-grid');
  const badge = $q('#technicals-signal');

  // Comprehensive signal analysis
  let bullish = 0, bearish = 0;
  const checks = [
    { val: technicals.price_vs_sma20, label: 'Price vs SMA 20', key: 'price_vs_sma20' },
    { val: technicals.price_vs_sma50, label: 'Price vs SMA 50', key: 'price_vs_sma50' },
    { val: technicals.price_vs_sma200, label: 'Price vs SMA 200', key: 'price_vs_sma200' },
    { val: technicals.rsi_14, label: 'RSI (14)', key: 'rsi_14', invert: true },
    { val: technicals.macd_histogram, label: 'MACD Hist', key: 'macd_histogram' },
    { val: technicals.bb_upper && technicals.current_price ? ((technicals.current_price - technicals.bb_middle) / (technicals.bb_upper - technicals.bb_middle) * 2 - 1) : null, label: 'BB Position', key: 'bb_position' }
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

  const currentPrice = technicals.current_price || quote?.price;
  const items = [
    // Momentum
    { label: 'RSI (14)', value: technicals.rsi_14, fmt: v => v?.toFixed(1), class: v => v > 70 ? 'negative' : v < 30 ? 'positive' : '', group: 'Momentum' },
    { label: 'MACD', value: technicals.macd, fmt: v => v?.toFixed(4), group: 'Momentum' },
    { label: 'Signal Line', value: technicals.macd_signal, fmt: v => v?.toFixed(4), group: 'Momentum' },
    { label: 'MACD Histogram', value: technicals.macd_histogram, fmt: v => v?.toFixed(4), class: v => v > 0 ? 'positive' : 'negative', group: 'Momentum' },

    // Moving Averages
    { label: 'SMA 20', value: technicals.sma_20, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma20 !== null ? `${technicals.price_vs_sma20 > 0 ? '+' : ''}${technicals.price_vs_sma20.toFixed(2)}%` : null, subClass: technicals.price_vs_sma20 > 0 ? 'positive' : 'negative', group: 'Moving Averages' },
    { label: 'SMA 50', value: technicals.sma_50, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma50 !== null ? `${technicals.price_vs_sma50 > 0 ? '+' : ''}${technicals.price_vs_sma50.toFixed(2)}%` : null, subClass: technicals.price_vs_sma50 > 0 ? 'positive' : 'negative', group: 'Moving Averages' },
    { label: 'SMA 200', value: technicals.sma_200, fmt: v => formatCurrency(v), sub: technicals.price_vs_sma200 !== null ? `${technicals.price_vs_sma200 > 0 ? '+' : ''}${technicals.price_vs_sma200.toFixed(2)}%` : null, subClass: technicals.price_vs_sma200 > 0 ? 'positive' : 'negative', group: 'Moving Averages' },

    // Bollinger Bands
    { label: 'BB Upper', value: technicals.bb_upper, fmt: v => formatCurrency(v), group: 'Bollinger Bands' },
    { label: 'BB Middle', value: technicals.bb_middle, fmt: v => formatCurrency(v), group: 'Bollinger Bands' },
    { label: 'BB Lower', value: technicals.bb_lower, fmt: v => formatCurrency(v), group: 'Bollinger Bands' },

    // Volatility & Volume
    { label: 'ATR (14)', value: technicals.atr_14, fmt: v => formatCurrency(v), group: 'Volatility' },
    { label: 'Avg Volume', value: technicals.avg_volume, fmt: v => formatNumber(v), group: 'Volume' },
    { label: 'Avg Vol (10d)', value: technicals.avg_volume_10d, fmt: v => formatNumber(v), group: 'Volume' },

    // Price Levels
    { label: '52W High', value: technicals['52wk_high'], fmt: v => formatCurrency(v), group: 'Price Levels' },
    { label: '52W Low', value: technicals['52wk_low'], fmt: v => formatCurrency(v), group: 'Price Levels' },
    { label: '50D Avg', value: technicals['50d_avg'], fmt: v => formatCurrency(v), group: 'Price Levels' },
    { label: '200D Avg', value: technicals['200d_avg'], fmt: v => formatCurrency(v), group: 'Price Levels' },
    { label: 'Beta', value: technicals.beta, fmt: v => v?.toFixed(2), group: 'Price Levels' },
  ];

  // Group items by group
  const grouped = items.reduce((acc, item) => {
    const g = item.group || 'Other';
    if (!acc[g]) acc[g] = [];
    acc[g].push(item);
    return acc;
  }, {});

  let html = '';
  for (const [group, groupItems] of Object.entries(grouped)) {
    html += `<div class="tech-group"><h4 class="tech-group__title">${group}</h4><div class="technicals-grid">`;
    html += groupItems.map(item => {
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
    html += '</div></div>';
  }

  grid.innerHTML = html;
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

// ===== Live Data Sync =====
let pollTimer = null;

function startPolling(ticker) {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/${ticker}`);
      if (res.ok) {
        const data = await res.json();
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
  $qa('.tech-item').forEach(item => {
    const label = item.querySelector('.tech-item__label')?.textContent;
    const valueEl = item.querySelector('.tech-item__value');
    const subEl = item.querySelector('.tech-item__sub');
    if (!label || !valueEl) return;

    const keyMap = {
      'RSI (14)': 'rsi_14',
      'MACD': 'macd',
      'Signal Line': 'macd_signal',
      'MACD Histogram': 'macd_histogram',
      'SMA 20': 'sma_20',
      'SMA 50': 'sma_50',
      'SMA 200': 'sma_200',
      'BB Upper': 'bb_upper',
      'BB Middle': 'bb_middle',
      'BB Lower': 'bb_lower',
      'ATR (14)': 'atr_14',
      'Avg Volume': 'avg_volume',
      'Avg Vol (10d)': 'avg_volume_10d',
      '52W High': '52wk_high',
      '52W Low': '52wk_low',
      '50D Avg': '50d_avg',
      '200D Avg': '200d_avg',
      'Beta': 'beta'
    };
    const key = keyMap[label.trim()];
    if (key && technicals[key] !== undefined) {
      const val = technicals[key];
      if (label.includes('SMA') || label.includes('BB') || label.includes('ATR') || label.includes('52W') || label.includes('50D') || label.includes('200D')) {
        valueEl.textContent = formatCurrency(val);
      } else if (label === 'RSI (14)' || label === 'Beta') {
        valueEl.textContent = val?.toFixed(2);
      } else if (label.includes('Volume')) {
        valueEl.textContent = formatNumber(val);
      } else {
        valueEl.textContent = val?.toFixed(4);
      }
    }
    // Update sub value for SMAs
    if (subEl && label.startsWith('SMA')) {
      const pctKey = `price_vs_sma${label.replace('SMA ', '')}`;
      if (technicals[pctKey] !== undefined) {
        const pct = technicals[pctKey];
        subEl.textContent = `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`;
        subEl.className = `tech-item__sub ${pct > 0 ? 'positive' : 'negative'}`;
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
    renderFilings(appState.data);
    renderOverview(appState.data);

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

  // Wire topbar actions
  $qa('.timeframe-btn').forEach(btn => {
    btn.addEventListener('click', () => handleTimeframeChange(btn));
  });

  $q('#ticker-select')?.addEventListener('change', (e) => handleTickerChange(e.target));

  $q('#refresh-btn')?.addEventListener('click', () => {
    if (appState.ticker) loadCompanyData(appState.ticker);
  });

  // Theme toggle
  $q('#theme-btn')?.addEventListener('click', () => {
    const html = document.documentElement;
    const newTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
    renderMiniChart();
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
    }, 150);
  });

  // Cleanup on unload
  window.addEventListener('beforeunload', stopPolling);

  // Handle browser back/forward
  window.addEventListener('popstate', async () => {
    const path = window.location.pathname;
    const match = path.match(/\/company\/([A-Za-z]+)/);
    if (match) {
      appState = { ...appState, data: null, charts: { mini: null } };
      await loadCompanyData(match[1].toUpperCase());
    }
  });
}

function handleTimeframeChange(btn) {
  const tf = btn.dataset.tf;
  if (tf === appState.currentTimeframe) return;

  $qa('.timeframe-btn').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
  btn.classList.add('active');
  btn.setAttribute('aria-pressed', 'true');
  appState.currentTimeframe = tf;
  renderMiniChart();
}

async function handleTickerChange(select) {
  const ticker = select.value;
  if (!ticker) return;
  await loadCompanyData(ticker);
}

// Start
document.addEventListener('DOMContentLoaded', init);