/**
 * FinGraph Company Page - Main Application
 * Fetches and displays comprehensive company data from Yahoo Finance
 */

// ===== Configuration =====
const API_BASE = '/api/company';
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
let state = {
  ticker: '',
  data: null,
  currentTimeframe: '1D',
  chartType: 'candlestick',
  activeIndicators: { sma20: true, sma50: true, bb: false, volume: true },
  activeFundTab: 'valuation',
  charts: {
    mini: null,
    main: null,
    kg: null
  }
};

// ===== DOM Elements =====
const els = {};

// ===== Utility Functions =====
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

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

const formatTimeAgo = (timestamp) => {
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
};

const showToast = (message, type = 'info') => {
  const container = $('#toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.innerHTML = `
    <span class="toast__message">${message}</span>
    <button class="toast__close" aria-label="Dismiss">&times;</button>
  `;
  toast.querySelector('.toast__close').addEventListener('click', () => toast.remove());
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
};

const setLoading = (show) => {
  $('#loading-overlay').hidden = !show;
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

// ===== Data Processing =====
function processChartData(chartData, timeframe) {
  if (!chartData) return { labels: [], prices: [], volumes: [], timestamps: [] };

  const timestamps = chartData.timestamp || [];
  const quotes = chartData.indicators?.quote?.[0] || {};
  const closes = quotes.close || [];
  const opens = quotes.open || [];
  const highs = quotes.high || [];
  const lows = quotes.low || [];
  const volumes = quotes.volume || [];

  // Filter valid data points
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
  const { data } = state;
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
  const upper = [];
  const middle = [];
  const lower = [];
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) {
      upper.push(null);
      middle.push(null);
      lower.push(null);
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

  $('#company-ticker').textContent = quote.ticker;
  $('#page-title').textContent = `${quote.ticker} - FinGraph`;
  $('#company-name').textContent = fundamentals.description?.split('.')[0] || `${quote.ticker} Inc.`;
  $('#company-exchange').textContent = quote.exchange || 'N/A';
  $('#company-sector').textContent = fundamentals.sector || 'N/A';
  $('#company-currency').textContent = quote.currency || 'USD';

  $('#ask-btn').dataset.ticker = quote.ticker;
  $('#ask-btn').href = `/app?ticker=${quote.ticker}`;
}

function renderLivePrice(data) {
  const { quote, technicals } = data;
  if (!quote) return;

  const priceEl = $('#current-price');
  const changeEl = $('#change-value');
  const stateEl = $('#market-state');

  priceEl.textContent = formatCurrency(quote.price, quote.currency);

  if (quote.change !== null && quote.pct !== null) {
    const isPositive = quote.change >= 0;
    changeEl.textContent = `${isPositive ? '+' : ''}${formatNumber(quote.change)} (${formatPct(quote.pct)})`;
    changeEl.className = `change-value ${isPositive ? 'positive' : 'negative'}`;
  } else {
    changeEl.textContent = '—';
    changeEl.className = 'change-value';
  }

  const marketStates = {
    'REGULAR': 'Market Open',
    'PRE': 'Pre-Market',
    'POST': 'After Hours',
    'CLOSED': 'Market Closed'
  };
  stateEl.textContent = marketStates[quote.market_state] || 'Market Closed';

  // Price stats
  const statsContainer = $('#price-stats');
  const stats = [
    { label: 'Open', value: quote.price }, // Would need from chart
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
  const chartData = getChartDataForTimeframe(state.currentTimeframe);
  if (!chartData || chartData.prices.length === 0) return;

  const ctx = document.getElementById('mini-chart').getContext('2d');
  const width = ctx.canvas.width = ctx.canvas.offsetWidth * devicePixelRatio;
  const height = ctx.canvas.height = ctx.canvas.offsetHeight * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);

  const cssWidth = ctx.canvas.offsetWidth;
  const cssHeight = ctx.canvas.offsetHeight;
  const prices = chartData.prices;
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const range = maxPrice - minPrice || 1;

  // Clear
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

  // Store for resize
  state.charts.mini = { ctx, data: chartData, cssWidth, cssHeight, minPrice, maxPrice };
}

function renderMainChart() {
  const chartData = getChartDataForTimeframe(state.currentTimeframe);
  if (!chartData || chartData.raw.length === 0) return;

  const canvas = $('#main-chart');
  const ctx = canvas.getContext('2d');

  // Destroy existing chart
  if (state.charts.main) {
    state.charts.main.destroy();
  }

  const prices = chartData.prices;
  const opens = chartData.opens;
  const highs = chartData.highs;
  const lows = chartData.lows;
  const volumes = chartData.volumes;

  const datasets = [];

  // Main price dataset
  if (state.chartType === 'candlestick') {
    datasets.push({
      label: 'Price',
      data: chartData.raw.map((d, i) => ({ x: d.t, o: opens[i], h: highs[i], l: lows[i], c: prices[i] })),
      type: 'candlestick',
      color: {
        up: CHART_COLORS.up,
        down: CHART_COLORS.down,
        unchanged: CHART_COLORS.up
      },
      borderColor: {
        up: CHART_COLORS.up,
        down: CHART_COLORS.down
      },
      wickColor: {
        up: CHART_COLORS.up,
        down: CHART_COLORS.down
      }
    });
  } else {
    datasets.push({
      label: 'Price',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: prices[i] })),
      type: state.chartType === 'area' ? 'line' : 'line',
      borderColor: CHART_COLORS.up,
      backgroundColor: state.chartType === 'area' ? 'rgba(0, 212, 170, 0.15)' : 'transparent',
      borderWidth: 2,
      fill: state.chartType === 'area',
      tension: 0.2,
      pointRadius: 0,
      pointHoverRadius: 4
    });
  }

  // SMA 20
  if (state.activeIndicators.sma20) {
    const sma20 = calculateSMA(prices, 20);
    datasets.push({
      label: 'SMA 20',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: sma20[i] })).filter(d => d.y !== null),
      type: 'line',
      borderColor: CHART_COLORS.sma20,
      borderWidth: 1.5,
      borderDash: [5, 5],
      fill: false,
      pointRadius: 0,
      tension: 0.1
    });
  }

  // SMA 50
  if (state.activeIndicators.sma50 && prices.length >= 50) {
    const sma50 = calculateSMA(prices, 50);
    datasets.push({
      label: 'SMA 50',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: sma50[i] })).filter(d => d.y !== null),
      type: 'line',
      borderColor: CHART_COLORS.sma50,
      borderWidth: 1.5,
      borderDash: [5, 5],
      fill: false,
      pointRadius: 0,
      tension: 0.1
    });
  }

  // SMA 200
  if (prices.length >= 200) {
    const sma200 = calculateSMA(prices, 200);
    datasets.push({
      label: 'SMA 200',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: sma200[i] })).filter(d => d.y !== null),
      type: 'line',
      borderColor: CHART_COLORS.sma200,
      borderWidth: 2,
      fill: false,
      pointRadius: 0,
      tension: 0.1
    });
  }

  // Bollinger Bands
  if (state.activeIndicators.bb && prices.length >= 20) {
    const bb = calculateBollingerBands(prices);
    datasets.push({
      label: 'BB Upper',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: bb.upper[i] })).filter(d => d.y !== null),
      type: 'line',
      borderColor: CHART_COLORS.bb,
      borderWidth: 1,
      borderDash: [3, 3],
      fill: false,
      pointRadius: 0
    });
    datasets.push({
      label: 'BB Lower',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: bb.lower[i] })).filter(d => d.y !== null),
      type: 'line',
      borderColor: CHART_COLORS.bb,
      borderWidth: 1,
      borderDash: [3, 3],
      fill: '-1', // Fill to previous dataset (upper)
      backgroundColor: CHART_COLORS.bb,
      pointRadius: 0
    });
  }

  // Volume
  if (state.activeIndicators.volume) {
    datasets.push({
      label: 'Volume',
      data: chartData.raw.map((d, i) => ({ x: d.t, y: volumes[i] })),
      type: 'bar',
      backgroundColor: chartData.raw.map((d, i) => prices[i] >= opens[i] ? 'rgba(0, 212, 170, 0.5)' : 'rgba(255, 60, 0, 0.5)'),
      borderColor: 'transparent',
      borderWidth: 0,
      yAxisID: 'y1',
      maxBarThickness: 8
    });
  }

  state.charts.main = new Chart(ctx, {
    type: 'candlestick',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index',
        intersect: false
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: CHART_COLORS.tooltipBg,
          borderColor: CHART_COLORS.tooltipBorder,
          borderWidth: 1,
          titleColor: '#fff',
          bodyColor: '#fff',
          padding: 12,
          cornerRadius: 8,
          displayColors: true,
          callbacks: {
            label: (ctx) => {
              if (ctx.dataset.type === 'candlestick') {
                const d = ctx.raw;
                return `O: ${formatNumber(d.o)} H: ${formatNumber(d.h)} L: ${formatNumber(d.l)} C: ${formatNumber(d.c)}`;
              }
              if (ctx.dataset.label === 'Volume') {
                return `Volume: ${formatNumber(ctx.raw.y)}`;
              }
              return `${ctx.dataset.label}: ${formatNumber(ctx.raw.y)}`;
            }
          }
        },
        crosshair: {
          line: { color: CHART_COLORS.grid, width: 1 },
          sync: { enabled: false }
        }
      },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'day', displayFormats: { day: 'MMM d' } },
          grid: { color: CHART_COLORS.grid, drawBorder: false },
          ticks: { color: CHART_COLORS.text, maxTicksLimit: 8, font: { family: 'var(--font-mono)', size: 10 } },
          border: { display: false }
        },
        y: {
          type: 'linear',
          position: 'right',
          grid: { color: CHART_COLORS.grid, drawBorder: false },
          ticks: { color: CHART_COLORS.text, font: { family: 'var(--font-mono)', size: 10 }, callback: v => formatNumber(v, 2) },
          border: { display: false }
        },
        y1: {
          type: 'linear',
          display: state.activeIndicators.volume,
          position: 'left',
          grid: { drawOnChartArea: false },
          ticks: { color: CHART_COLORS.text, font: { family: 'var(--font-mono)', size: 10 }, callback: v => formatNumber(v, 0) },
          border: { display: false },
          min: 0
        }
      }
    }
  });

  renderChartLegend();
}

function renderChartLegend() {
  const legend = $('#chart-legend');
  if (!legend) return;

  const items = [];
  if (state.chartType === 'candlestick') {
    items.push({ label: 'Price', color: CHART_COLORS.up });
  } else {
    items.push({ label: 'Price', color: CHART_COLORS.up });
  }
  if (state.activeIndicators.sma20) items.push({ label: 'SMA 20', color: CHART_COLORS.sma20 });
  if (state.activeIndicators.sma50) items.push({ label: 'SMA 50', color: CHART_COLORS.sma50 });
  if (state.activeIndicators.bb) items.push({ label: 'Bollinger Bands', color: CHART_COLORS.bb });
  if (state.activeIndicators.volume) items.push({ label: 'Volume', color: CHART_COLORS.volume });

  legend.innerHTML = items.map(item => `
    <span class="legend-item">
      <span class="legend-color" style="background: ${item.color}"></span>
      ${item.label}
    </span>
  `).join('');
}

function renderTechnicals(data) {
  const { technicals, quote } = data;
  if (!technicals) return;

  const grid = $('#technicals-grid');
  const badge = $('#technicals-signal');

  // Determine overall signal
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

  const panels = $('#fundamentals-panels');

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
    <div class="fund-panel ${tabKey === state.activeFundTab ? 'active' : ''}" data-tab="${tabKey}" role="tabpanel">
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

  // Tab click handlers
  $$('.fund-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.fund-tab').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      $$('.fund-panel').forEach(p => p.classList.remove('active'));
      $(`[data-tab="${btn.dataset.tab}"]`).classList.add('active');
      state.activeFundTab = btn.dataset.tab;
    });
  });
}

function renderNews(data) {
  const { news } = data;
  const list = $('#news-list');

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

function renderKnowledgeGraph(data) {
  const { graph_info } = data;
  const canvas = $('#kg-canvas');
  const empty = $('#kg-empty');
  const legend = $('#kg-legend');

  if (!graph_info?.in_graph) {
    canvas.hidden = true;
    empty.hidden = false;
    legend.hidden = true;
    return;
  }

  canvas.hidden = false;
  empty.hidden = true;
  legend.hidden = false;

  // Simple force-directed graph visualization
  const ctx = canvas.getContext('2d');
  const width = canvas.width = canvas.offsetWidth * devicePixelRatio;
  const height = canvas.height = canvas.offsetHeight * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const cssWidth = canvas.offsetWidth;
  const cssHeight = canvas.offsetHeight;

  // Mock nodes for visualization (company + related entities)
  const nodes = [
    { id: state.ticker, label: state.ticker, type: 'company', x: cssWidth / 2, y: cssHeight / 2, fx: cssWidth / 2, fy: cssHeight / 2 },
    { id: 'sector', label: data.fundamentals.sector || 'Technology', type: 'sector', x: cssWidth / 2 + 150, y: cssHeight / 2 - 80 },
    { id: 'industry', label: data.fundamentals.industry || 'Software', type: 'industry', x: cssWidth / 2 - 150, y: cssHeight / 2 - 80 },
    { id: 'filings', label: `${graph_info.filings?.length || 0} Filings`, type: 'filings', x: cssWidth / 2 + 100, y: cssHeight / 2 + 100 },
    { id: 'peers', label: 'Peers', type: 'peers', x: cssWidth / 2 - 100, y: cssHeight / 2 + 100 }
  ];

  const links = [
    { source: state.ticker, target: 'sector' },
    { source: state.ticker, target: 'industry' },
    { source: state.ticker, target: 'filings' },
    { source: state.ticker, target: 'peers' }
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

    // Outer glow
    const gradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, radius + 10);
    gradient.addColorStop(0, color + '40');
    gradient.addColorStop(1, 'transparent');
    ctx.beginPath();
    ctx.arc(node.x, node.y, radius + 10, 0, Math.PI * 2);
    ctx.fillStyle = gradient;
    ctx.fill();

    // Node circle
    ctx.beginPath();
    ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();

    // Border
    ctx.strokeStyle = '#030303';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Label
    ctx.font = '500 12px var(--font-sans)';
    ctx.fillStyle = '#fff';
    ctx.textAlign = 'center';
    ctx.fillText(node.label, node.x, node.y + radius + 18);
  });

  // Legend
  legend.innerHTML = Object.entries(typeColors).map(([type, color]) => `
    <span class="kg-legend-item">
      <span class="kg-legend-color" style="background: ${color}"></span>
      ${type.charAt(0).toUpperCase() + type.slice(1)}
    </span>
  `).join('');
}

function renderDescription(data) {
  const { fundamentals } = data;
  const content = $('#description-content');
  const meta = $('#description-meta');

  if (fundamentals.description) {
    content.innerHTML = `<p>${fundamentals.description}</p>`;
  } else {
    content.innerHTML = '<p style="color:var(--color-text-muted)">No description available.</p>';
  }

  const metaItems = [
    { label: 'Sector', value: fundamentals.sector },
    { label: 'Industry', value: fundamentals.industry },
    { label: 'Employees', value: fundamentals.employees ? fundamentals.employees.toLocaleString() : null },
    { label: 'Country', value: fundamentals.country },
    { label: 'Website', value: fundamentals.website ? `<a href="${fundamentals.website}" target="_blank" rel="noopener" style="color:var(--color-primary)">${fundamentals.website}</a>` : null }
  ].filter(m => m.value);

  meta.innerHTML = metaItems.map(m => `
    <div class="meta-item">
      <span class="meta-item__label">${m.label}</span>
      <span class="meta-item__value">${m.value}</span>
    </div>
  `).join('');
}

function renderFilings(data) {
  const { graph_info } = data;
  const section = $('#filings-section');
  const tbody = $('#filings-body');

  if (!graph_info?.in_graph || !graph_info.filings?.length) {
    section.hidden = true;
    return;
  }

  section.hidden = false;
  tbody.innerHTML = graph_info.filings.map(f => `
    <tr>
      <td><span class="form-type">${f.form}</span></td>
      <td>${f.fiscal_year}</td>
      <td>${f.period === 'FY' ? 'Full Year' : 'Q' + f.period}</td>
      <td>${formatDate(f.period_end)}</td>
    </tr>
  `).join('');
}

// ===== Event Handlers =====
function handleTimeframeChange(btn) {
  const tf = btn.dataset.tf;
  if (tf === state.currentTimeframe) return;

  $$('.timeframe-btn').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
  btn.classList.add('active');
  btn.setAttribute('aria-pressed', 'true');
  state.currentTimeframe = tf;

  renderMiniChart();
  renderMainChart();
}

function handleChartTypeChange(radio) {
  state.chartType = radio.value;
  renderMainChart();
}

function handleIndicatorChange(checkbox) {
  state.activeIndicators[checkbox.value] = checkbox.checked;
  renderMainChart();
}

// ===== Initialization =====
function initElements() {
  // Navigation
  els.hamburger = $('.nav__hamburger');
  els.mobileMenu = $('#nav-menu');
  els.themeToggle = $('[data-theme-toggle]');

  // Timeframe buttons
  $$('.timeframe-btn').forEach(btn => {
    btn.addEventListener('click', () => handleTimeframeChange(btn));
  });

  // Chart type radios
  $$('input[name="chart-type"]').forEach(radio => {
    radio.addEventListener('change', () => handleChartTypeChange(radio));
  });

  // Indicator checkboxes
  $$('input[name="indicator"]').forEach(cb => {
    cb.addEventListener('change', () => handleIndicatorChange(cb));
  });

  // Mobile menu
  els.hamburger.addEventListener('click', () => {
    const expanded = els.hamburger.getAttribute('aria-expanded') === 'true';
    els.hamburger.setAttribute('aria-expanded', !expanded);
    els.mobileMenu.hidden = expanded;
  });

  // Theme toggle
  els.themeToggle.addEventListener('click', () => {
    const html = document.documentElement;
    const newTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
    renderMiniChart();
    renderMainChart();
  });

  // Watchlist button
  $('#watchlist-btn')?.addEventListener('click', () => {
    showToast(`${state.ticker} added to watchlist`, 'success');
  });

  // Close mobile menu on link click
  $$('.nav__mobile .nav__link, .nav__mobile .nav__signin').forEach(link => {
    link.addEventListener('click', () => {
      els.hamburger.setAttribute('aria-expanded', 'false');
      els.mobileMenu.hidden = true;
    });
  });
}

async function loadCompanyData() {
  // Extract ticker from URL
  const path = window.location.pathname;
  const match = path.match(/\/company\/([A-Za-z]+)/);
  if (!match) {
    showToast('Invalid company ticker', 'error');
    return;
  }

  state.ticker = match[1].toUpperCase();
  setLoading(true);

  try {
    state.data = await fetchCompanyData(state.ticker);
    if (!state.data) throw new Error('No data returned');

    // Render all sections
    renderCompanyIdentity(state.data);
    renderLivePrice(state.data);
    renderMiniChart();
    renderMainChart();
    renderTechnicals(state.data);
    renderFundamentals(state.data);
    renderNews(state.data);
    renderKnowledgeGraph(state.data);
    renderDescription(state.data);
    renderFilings(state.data);

  } catch (err) {
    console.error('Failed to load company data:', err);
    showToast(`Failed to load ${state.ticker}: ${err.message}`, 'error');
  } finally {
    setLoading(false);
  }
}

function handleResize() {
  // Re-render charts on resize
  if (state.charts.mini) renderMiniChart();
  if (state.charts.main) renderMainChart();
  if (state.charts.kg) renderKnowledgeGraph(state.data);
}

// ===== Main =====
document.addEventListener('DOMContentLoaded', () => {
  initElements();
  loadCompanyData();

  // Handle resize with debounce
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(handleResize, 150);
  });

  // Theme from localStorage
  const savedTheme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', savedTheme);
});

// Handle browser back/forward
window.addEventListener('popstate', () => {
  state = { ...state, data: null, charts: { mini: null, main: null, kg: null } };
  loadCompanyData();
});