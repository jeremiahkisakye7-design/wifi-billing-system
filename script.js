const API_BASE = '/api';
const IS_STATIC_DEPLOYMENT = window.location.protocol === 'file:' || window.location.hostname.endsWith('netlify.app');

const PLAN_PRICES = {
  shortSession: 500,
  fullDay: 1000,
  weekly: 4000,
  monthly: 25000,
};

const PLAN_DURATIONS_MS = {
  shortSession: 6 * 60 * 60 * 1000,
  fullDay: 24 * 60 * 60 * 1000,
  weekly: 7 * 24 * 60 * 60 * 1000,
  monthly: 30 * 24 * 60 * 60 * 1000,
};

const ADDON_PRICES = {
  router: 15000,
  installation: 10000,
  speed: 12000,
  support: 8000,
};

const DEVICE_PRICE = 2500;
const TAX_RATE = 0.15;
const SERVICE_CHARGE_RATE = 0.05;
const MOBILE_MONEY_USSD_PREFIX = {
  momo: '*165*1*1',
  airtel: '*185*1*1',
};
const MOBILE_MONEY_WEB = {
  momo: 'https://www.mtn.co.ug/momo/',
  airtel: 'https://www.airtel.co.ug/airtel-money',
};
const MOBILE_MONEY_APP = {
  momo: 'mtnmomo://send',
  airtel: 'airtelmoney://send',
};
const PAYMENT_SETTINGS_KEY = 'wifiBillingPaymentSettings';
const VOUCHERS_KEY = 'wifiBillingVouchers';
const PAYMENT_INTENT_KEY = 'wifiBillingPaymentIntent';
const DASHBOARD_KEY = 'wifiBillingActiveVoucher';
const DEFAULT_PAYMENT_SETTINGS = { method: 'ussd' };
const SPEED_TIER_MULTIPLIER = { basic: 1, premium: 1.6 };
let publicSpeedTier = 'basic';
const SUPPORT_INQUIRY_NUMBER_DEFAULT = '0704270565';
let SUPPORT_INQUIRY_NUMBER = SUPPORT_INQUIRY_NUMBER_DEFAULT;
const deviceParams = new URLSearchParams(window.location.search);
const DEVICE_MAC = deviceParams.get('mac') || deviceParams.get('client_mac') || deviceParams.get('device_mac') || '';
let publicPlan = 'fullDay';
let deferredInstallPrompt;

const form = document.getElementById('billingForm');
const loginOverlay = document.getElementById('loginOverlay');
const publicPortal = document.getElementById('publicPortal');
const adminApp = document.getElementById('adminApp');
const loginForm = document.getElementById('loginForm');
const adminAccessBtn = document.getElementById('adminAccessBtn');
const cancelAdminBtn = document.getElementById('cancelAdminBtn');
const publicProviderInput = document.getElementById('publicProvider');
const publicPhoneInput = document.getElementById('publicPhone');
const publicPaymentStatus = document.getElementById('publicPaymentStatus');
const publicPlanTotalEl = document.getElementById('publicPlanTotal');
const publicServiceChargeEl = document.getElementById('publicServiceCharge');
const publicAmountDueEl = document.getElementById('publicAmountDue');
const voucherInput = document.getElementById('voucherInput');
const redeemVoucherBtn = document.getElementById('redeemVoucherBtn');
const voucherStatus = document.getElementById('voucherStatus');
const dashboardPanel = document.getElementById('customerDashboard');
const dashboardVoucherEl = document.getElementById('dashboardVoucher');
const dashboardPlanEl = document.getElementById('dashboardPlan');
const dashboardTimeEl = document.getElementById('dashboardTime');
const dashboardStatusEl = document.getElementById('dashboardStatus');
const dashboardRenewBtn = document.getElementById('dashboardRenewBtn');
const paymentSettingsForm = document.getElementById('paymentSettingsForm');
const paymentMethodSetting = document.getElementById('paymentMethodSetting');
const paymentSettingsStatus = document.getElementById('paymentSettingsStatus');
const logoutBtn = document.getElementById('logoutBtn');
const refreshBtn = document.getElementById('refreshBtn');
const printInvoiceBtn = document.getElementById('printInvoiceBtn');
const tableBody = document.getElementById('billingTableBody');
const searchInput = document.getElementById('searchInput');
const resetBtn = document.getElementById('resetBtn');
const addUserForm = document.getElementById('addUserForm');
const expiringList = document.getElementById('expiringList');
const monthlyReportList = document.getElementById('monthlyReportList');
const adminUsersList = document.getElementById('adminUsersList');
const addRouterBtn = document.getElementById('addRouterBtn');
const routersListEl = document.getElementById('routersList');
const connectWiFiBtn = document.getElementById('connectWiFiBtn');
const wifiNetworksListEl = document.getElementById('wifiNetworksList');
const invoicesListEl = document.getElementById('invoicesList');
const viewAllInvoicesBtn = document.getElementById('viewAllInvoicesBtn');
const protectedAccessForm = document.getElementById('protectedAccessForm');
const protectedInfoModal = document.getElementById('protectedInfoModal');
const protectedPasswordInput = document.getElementById('protectedPassword');
const installAppBtn = document.getElementById('installAppBtn');
const publicInstallAppBtn = document.getElementById('publicInstallAppBtn');

const customerNameInput = document.getElementById('customerName');
const customerPhoneInput = document.getElementById('customerPhone');
const planTypeInput = document.getElementById('planType');
const paymentMethodInput = document.getElementById('paymentMethod');
const deviceCountInput = document.getElementById('deviceCount');
const startDateInput = document.getElementById('startDate');
const discountInput = document.getElementById('discount');
const routerOptionInput = document.getElementById('routerOption');

const baseCostEl = document.getElementById('baseCost');
const deviceCostEl = document.getElementById('deviceCost');
const addonCostEl = document.getElementById('addonCost');
const taxCostEl = document.getElementById('taxCost');
const discountValueEl = document.getElementById('discountValue');
const totalDueEl = document.getElementById('totalDue');
const billNumberEl = document.getElementById('billNumber');

const revenueStatEl = document.getElementById('revenueStat');
const activeClientsStatEl = document.getElementById('activeClientsStat');
const pendingStatEl = document.getElementById('pendingStat');
const todayStatEl = document.getElementById('todayStat');
const todaySalesCardEl = document.getElementById('todaySalesCard');
const unpaidCardEl = document.getElementById('unpaidCard');
const cashFlowCardEl = document.getElementById('cashFlowCard');

const currency = new Intl.NumberFormat('en-UG', {
  style: 'currency',
  currency: 'UGX',
  maximumFractionDigits: 0,
});

let isAdminAuthenticated = false;

const adminUsers = [
  'admin',
  'manager',
  'cashier',
];

let routers = [
  { id: 'router-1', name: 'Main Router', model: 'TP-Link Archer C7', ip: '192.168.1.1', status: 'online', clients: 8 },
  { id: 'router-2', name: 'Backup Router', model: 'D-Link DIR-882', ip: '192.168.1.2', status: 'online', clients: 3 },
  {
    id: 'router-cpe-b07',
    name: 'CPE_BE97',
    model: 'B07',
    ip: '192.168.100.1',
    mac: '74:F8:D6:3B:E9:7D',
    imei: '353899266529310',
    serialNumber: 'GUVECM160224',
    power: '5V / 2A',
    captivePortalUrl: window.location.origin,
    status: 'online',
    clients: 0,
  },
];

const wifiNetworks = [
  { id: 'net-1', name: 'Instant WiFi', signal: 95, speed: '5G', security: 'WPA3' },
  { id: 'net-2', name: 'Guest Network', signal: 80, speed: '2.4G', security: 'WPA2' },
  { id: 'net-3', name: 'Premium Access', signal: 100, speed: '5G', security: 'WPA3' },
];

const defaultRecords = [
  {
    id: crypto.randomUUID(),
    customerName: 'Nansubuga Aisha',
    phone: '+256700111222',
    plan: 'monthly',
    paymentMethod: 'Mobile Money',
    deviceCount: 2,
    date: new Date().toISOString().slice(0, 10),
    amount: 26625,
    discount: 500,
    status: 'paid',
    addons: ['router'],
  },
  {
    id: crypto.randomUUID(),
    customerName: 'Kato Daniel',
    phone: '+256784555777',
    plan: 'weekly',
    paymentMethod: 'Cash',
    deviceCount: 1,
    date: new Date().toISOString().slice(0, 10),
    amount: 7450,
    discount: 0,
    status: 'pending',
    addons: ['speed'],
  },
];

let records = [...defaultRecords];

function getRecordExpiry(record) {
  if (record.expiresAt) return new Date(record.expiresAt);
  const start = new Date(`${record.date}T00:00:00`);
  return new Date(start.getTime() + (PLAN_DURATIONS_MS[record.plan] || PLAN_DURATIONS_MS.monthly));
}

function getVouchers() {
  try {
    return JSON.parse(localStorage.getItem(VOUCHERS_KEY)) || {};
  } catch (error) {
    return {};
  }
}

function createVoucher(record) {
  const code = `WIFI-${crypto.randomUUID().slice(0, 8).toUpperCase()}`;
  const expiresAt = new Date(Date.now() + (PLAN_DURATIONS_MS[record.plan] || PLAN_DURATIONS_MS.monthly)).toISOString();
  const vouchers = getVouchers();
  vouchers[code] = { code, plan: record.plan, expiresAt, used: false };
  localStorage.setItem(VOUCHERS_KEY, JSON.stringify(vouchers));
  record.voucherCode = code;
  record.expiresAt = expiresAt;
  return code;
}

async function redeemVoucher() {
  const code = voucherInput.value.trim().toUpperCase();
  if (!code) {
    voucherStatus.textContent = 'Enter a voucher code first.';
    return;
  }
  if (!IS_STATIC_DEPLOYMENT) {
    try {
      const response = await fetch(`${API_BASE}/dashboard?voucher=${encodeURIComponent(code)}`);
      const data = await response.json();
      if (response.ok && data.isActive) {
        localStorage.setItem(DASHBOARD_KEY, JSON.stringify({ voucherCode: code }));
        voucherStatus.textContent = `WiFi access active. See your remaining time below.`;
        renderDashboard({ voucherCode: code, plan: data.plan, expiresAt: data.expires_at });
        return;
      }
      voucherStatus.textContent = data.error || 'This voucher is invalid or has expired.';
      return;
    } catch (error) {
      console.warn('Voucher lookup unavailable', error);
      voucherStatus.textContent = 'Could not verify the voucher right now. Please try again.';
      return;
    }
  }
  const voucher = getVouchers()[code];
  if (!voucher) {
    voucherStatus.textContent = 'Voucher not found. Ask the admin to confirm payment.';
    return;
  }
  if (voucher.used || new Date(voucher.expiresAt).getTime() <= Date.now()) {
    voucherStatus.textContent = 'This voucher has expired.';
    return;
  }
  const remainingMinutes = Math.max(1, Math.ceil((new Date(voucher.expiresAt).getTime() - Date.now()) / 60000));
  voucherStatus.textContent = `WiFi access approved for ${remainingMinutes} more minute(s). Connect to the WiFi network.`;
}

function expireRecords() {
  const now = Date.now();
  let changed = false;
  records = records.map((record) => {
    if (record.status === 'paid' && getRecordExpiry(record).getTime() <= now && record.status !== 'expired') {
      changed = true;
      return { ...record, status: 'expired' };
    }
    return record;
  });
  if (changed && IS_STATIC_DEPLOYMENT) localStorage.setItem('wifiBillingRecords', JSON.stringify(records));
}

async function fetchRecords() {
  if (IS_STATIC_DEPLOYMENT) {
    const savedRecords = localStorage.getItem('wifiBillingRecords');
    records = savedRecords ? JSON.parse(savedRecords) : [...defaultRecords];
    expireRecords();
    renderAll();
    return;
  }

  try {
    const response = await fetch(`${API_BASE}/clients`);
    if (!response.ok) throw new Error('Failed to load records');
    const result = await response.json();
    records = Array.isArray(result) ? result : [...defaultRecords];
    expireRecords();
    renderAll();
  } catch (error) {
    console.error(error);
    const savedRecords = localStorage.getItem('wifiBillingRecords');
    records = savedRecords ? JSON.parse(savedRecords) : [...defaultRecords];
    expireRecords();
    renderAll();
  }
}

async function restoreAdminSession() {
  if (IS_STATIC_DEPLOYMENT) return;
  try {
    const response = await fetch(`${API_BASE}/auth/session`);
    if (!response.ok || !(await response.json()).authenticated) return;
    publicPortal.classList.add('hidden');
    loginOverlay.classList.add('hidden');
    adminApp.classList.remove('hidden');
    logoutBtn.classList.remove('hidden');
    await fetchRecords();
    await fetchRouters();
  } catch (error) {
    console.warn('Could not restore admin session', error);
  }
}

async function saveRecord(record) {
  try {
    const payload = {
      id: record.id,
      customerName: record.customerName,
      phone: record.phone,
      plan: record.plan,
      paymentMethod: record.paymentMethod,
      deviceCount: record.deviceCount,
      date: record.date,
      amount: record.amount,
      discount: record.discount,
      status: record.status,
      addons: record.addons,
      routerId: record.routerId || '',
    };

    if (IS_STATIC_DEPLOYMENT) {
      records.push(record);
      localStorage.setItem('wifiBillingRecords', JSON.stringify(records));
      renderAll();
      return true;
    }

    const response = await fetch(`${API_BASE}/clients`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      if (response.status === 401) {
        logoutBtn.click();
        alert('Your admin session expired. Please log in again.');
        return false;
      }
      const errorText = await response.text();
      throw new Error(errorText || 'Save failed');
    }

    await fetchRecords();
    return true;
  } catch (error) {
    console.error(error);
    alert('Could not save the client record.');
    return false;
  }
}

function formatCurrency(value) {
  return currency.format(value);
}

function formatPlanName(plan) {
  return plan.charAt(0).toUpperCase() + plan.slice(1);
}

function getSelectedAddons() {
  return [...document.querySelectorAll('.addon:checked')].map((item) => item.value);
}

function safeDateValue() {
  return startDateInput.value || new Date().toISOString().slice(0, 10);
}

function calculateBill() {
  const plan = planTypeInput.value;
  const deviceCount = Number(deviceCountInput.value) || 1;
  const selectedAddons = getSelectedAddons();
  const basePlan = PLAN_PRICES[plan] || PLAN_PRICES.monthly;
  const deviceCost = deviceCount * DEVICE_PRICE;
  const addonsCost = selectedAddons.reduce((total, addon) => total + (ADDON_PRICES[addon] || 0), 0);
  const subtotal = basePlan + deviceCost + addonsCost;
  const tax = subtotal * TAX_RATE;
  const discount = Number(discountInput.value) || 0;
  const total = subtotal + tax - discount;

  baseCostEl.textContent = formatCurrency(basePlan);
  deviceCostEl.textContent = isAdminAuthenticated ? formatCurrency(deviceCost) : '🔒';
  addonCostEl.textContent = formatCurrency(addonsCost);
  taxCostEl.textContent = formatCurrency(tax);
  discountValueEl.textContent = formatCurrency(discount);
  totalDueEl.textContent = formatCurrency(total);
  billNumberEl.textContent = `BILL-${String(records.length + 1).padStart(3, '0')}`;
  return { basePlan, deviceCost, addonsCost, tax, discount, total, selectedAddons };
}

function renderStats() {
  const paidTotal = records.reduce((sum, record) => sum + (record.status === 'paid' ? record.amount : 0), 0);
  const pendingTotal = records.filter((record) => record.status === 'pending').length;
  const activeClients = records.length;
  const paidToday = records.filter((record) => {
    const today = new Date().toISOString().slice(0, 10);
    return record.date === today && record.status === 'paid';
  });
  const todaySales = paidToday.reduce((sum, record) => sum + record.amount, 0);

  revenueStatEl.textContent = isAdminAuthenticated ? formatCurrency(paidTotal) : '🔒 Restricted';
  activeClientsStatEl.textContent = activeClients;
  pendingStatEl.textContent = pendingTotal;
  todayStatEl.textContent = paidToday.length;
  todaySalesCardEl.textContent = isAdminAuthenticated ? formatCurrency(todaySales) : '🔒 Restricted';
  unpaidCardEl.textContent = pendingTotal;
  cashFlowCardEl.textContent = isAdminAuthenticated ? formatCurrency(paidTotal) : '🔒 Restricted';
}

function renderReports() {
  const expiring = [
    'Aisha M. – Monthly plan expires in 2 days',
    'Joseph K. – Weekly plan expires today',
    'Sarah N. – Full Day plan expires in 1 day',
  ];

  const monthly = [
    'Total customers: 128',
    'Revenue this month: UGX 4,850,000',
    'Pending payments: 12',
  ];

  expiringList.innerHTML = expiring.map((item) => `<li>${item}</li>`).join('');
  monthlyReportList.innerHTML = monthly.map((item) => `<li>${item}</li>`).join('');
  adminUsersList.innerHTML = adminUsers.map((user) => `<li>${user}</li>`).join('');
  renderRouters();
  renderWiFiNetworks();
  renderInvoices();
}

function renderRouters() {
  routersListEl.innerHTML = routers.map((router) => `
    <div class="router-card">
      <div class="router-header">
        <h3>${router.name}</h3>
        <span class="status-badge ${router.status === 'online' ? 'online' : 'offline'}">${router.status}</span>
      </div>
      <div class="router-details">
        <p><strong>Model:</strong> ${router.model}</p>
        <p><strong>IP:</strong> ${router.ip}</p>
        ${router.mac ? `<p><strong>SSID:</strong> ${router.name}</p><p><strong>MAC:</strong> ${router.mac}</p><p><strong>IMEI:</strong> ${router.imei}</p><p><strong>S/N:</strong> ${router.serialNumber}</p><p><strong>Power:</strong> ${router.power}</p>` : ''}
        ${router.captivePortalUrl ? `<p><strong>Captive portal:</strong> ${router.captivePortalUrl}</p>` : ''}
        <p><strong>Active clients:</strong> ${router.clients}</p>
      </div>
      <div class="router-actions">
        <button class="action-btn" onclick="editRouter('${router.id}')">Edit</button>
        <button class="action-btn delete" onclick="deleteRouter('${router.id}')">Remove</button>
      </div>
    </div>
  `).join('');
  if (routerOptionInput) {
    const selectedRouter = routerOptionInput.value;
    routerOptionInput.innerHTML = '<option value="">No router selected</option>' + routers
      .map((router) => `<option value="${router.id}">${router.name} (${router.ip})</option>`)
      .join('');
    routerOptionInput.value = routers.some((router) => router.id === selectedRouter) ? selectedRouter : '';
  }
}

async function fetchRouters() {
  if (IS_STATIC_DEPLOYMENT) {
    renderRouters();
    return;
  }
  try {
    const response = await fetch(`${API_BASE}/routers`);
    if (!response.ok) throw new Error('Failed to load routers');
    routers = await response.json();
    renderRouters();
  } catch (error) {
    console.error(error);
    renderRouters();
  }
}

async function editRouter(routerId) {
  const router = routers.find(r => r.id === routerId);
  if (!router) return;
  const newName = prompt('Router name:', router.name);
  if (!newName) return;
  if (IS_STATIC_DEPLOYMENT) {
    router.name = newName;
  } else {
    const response = await fetch(`${API_BASE}/routers/${routerId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    });
    if (!response.ok) return;
    await fetchRouters();
    return;
  }
  renderRouters();
}

async function deleteRouter(routerId) {
  if (confirm('Remove this router?')) {
    if (IS_STATIC_DEPLOYMENT) {
      routers = routers.filter(r => r.id !== routerId);
    } else {
      const response = await fetch(`${API_BASE}/routers/${routerId}`, { method: 'DELETE' });
      if (!response.ok) return;
      await fetchRouters();
      return;
    }
    renderRouters();
  }
}

async function addNewRouter() {
  const name = prompt('Enter router name:');
  if (!name) return;
  const model = prompt('Enter router model:');
  if (!model) return;
  const ip = prompt('Enter router IP (e.g., 192.168.1.1):');
  if (!ip) return;
  if (IS_STATIC_DEPLOYMENT) {
    routers.push({ id: `router-${Date.now()}`, name, model, ip, status: 'online', clients: 0 });
  } else {
    const response = await fetch(`${API_BASE}/routers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, model, ip }),
    });
    if (!response.ok) return;
    await fetchRouters();
    return;
  }
  renderRouters();
}

function renderWiFiNetworks() {
  wifiNetworksListEl.innerHTML = wifiNetworks.map((net) => `
    <div class="wifi-network-card">
      <div class="network-header">
        <h3>${net.name}</h3>
        <span class="signal-strength">${net.signal}%</span>
      </div>
      <div class="network-info">
        <span class="badge">${net.speed}</span>
        <span class="badge security">${net.security}</span>
      </div>
      <button class="action-btn" onclick="connectToNetwork('${net.id}')">Connect</button>
    </div>
  `).join('');
}

function connectToNetwork(networkId) {
  const network = wifiNetworks.find(n => n.id === networkId);
  if (network) {
    localStorage.setItem('wifiBillingRecords', JSON.stringify(records));
    return true;
  }
}

function renderInvoices() {
  const recentInvoices = records.slice(-5).reverse().map(record => ({
    ...record,
    invoiceNo: `INV-${record.id.slice(0, 8).toUpperCase()}`,
    date: record.date,
  }));

  if (!recentInvoices.length) {
    invoicesListEl.innerHTML = '<p class="empty-state">No invoices yet</p>';
    return;
  }

  invoicesListEl.innerHTML = recentInvoices.map(inv => `
    <div class="invoice-row">
      <div class="invoice-info">
        <strong>${inv.invoiceNo}</strong>
        <span class="invoice-customer">${inv.customerName}</span>
      </div>
      <div class="invoice-details">
        <span>${inv.date}</span>
        <strong class="invoice-amount">${formatCurrency(inv.amount)}</strong>
      </div>
      <span class="status-badge ${inv.status === 'paid' ? 'paid' : 'pending'}">${inv.status}</span>
    </div>
  `).join('');
}

async function sendPaymentDetails(method, amount) {
  const customerPhone = customerPhoneInput.value.trim() || 'Not provided';
  const recipient = await fetchAdminPaymentRecipient();
  if (!recipient) return;
  const message = `WiFi payment request via ${method}. Customer phone: ${customerPhone}. Amount: ${formatCurrency(amount)}. Please send the payment prompt.`;
  window.location.href = `sms:+256${recipient.slice(1)}?body=${encodeURIComponent(message)}`;
}

async function fetchAdminPaymentRecipient() {
  try {
    const response = await fetch(`${API_BASE}/admin/payment-recipient`);
    if (!response.ok) throw new Error('Unavailable');
    const data = await response.json();
    return data.recipient;
  } catch (error) {
    console.error(error);
    alert('Could not load the payment recipient. Please log in as admin and try again.');
    return '';
  }
}

function launchMobileMoney() {
  openMobileMoneyPortal('momo');
}

async function openMobileMoneyPortal(provider) {
  const customerName = customerNameInput.value.trim();
  const customerPhone = customerPhoneInput.value.trim();
  if (!customerName || !customerPhone) {
    alert('Enter the customer name and phone number first.');
    customerNameInput.focus();
    return;
  }

  paymentMethodInput.value = provider === 'momo' ? 'Mobile Money' : 'Mobile Money';
  const total = Math.max(0, Math.round(calculateBill().total));
  const recipient = await fetchAdminPaymentRecipient();
  if (!recipient) return;
  const ussdCode = `${MOBILE_MONEY_USSD_PREFIX[provider]}*${recipient}*${total}#`;
  window.location.href = `tel:${encodeURIComponent(ussdCode)}`;
}

let paymentPollTimer = null;
let dashboardCountdownTimer = null;

async function openPublicMobileMoney(provider) {
  const phone = publicPhoneInput?.value.trim();
  if (!phone) {
    publicPaymentStatus.textContent = 'Enter your phone number before paying.';
    publicPhoneInput?.focus();
    return;
  }
  const planPrice = PLAN_PRICES[publicPlan];
  const serviceCharge = Math.ceil(planPrice * SERVICE_CHARGE_RATE);
  const total = planPrice + serviceCharge;

  if (IS_STATIC_DEPLOYMENT) {
    publicPaymentStatus.textContent = `This demo page cannot process payment. Call support at ${SUPPORT_INQUIRY_NUMBER} to pay.`;
    return;
  }

  clearInterval(paymentPollTimer);
  document.getElementById('publicPayBtn').disabled = true;
  publicPaymentStatus.textContent = 'Sending payment request...';
  try {
    const response = await fetch(`${API_BASE}/payments/checkout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan: publicPlan,
        phone,
        provider,
        deviceMac: DEVICE_MAC,
        speedTier: publicSpeedTier,
        autoRenew: document.getElementById('publicAutoRenew')?.checked || false,
      }),
    });
    const intent = await response.json();
    if (!response.ok) throw new Error(intent.error || 'Could not start payment');
    localStorage.setItem(PAYMENT_INTENT_KEY, intent.paymentIntentId);
    publicPaymentStatus.textContent = intent.message || `Payment request created for ${formatCurrency(intent.amount)}.`;

    if (intent.mode === 'manual') {
      await launchManualUssd(intent.paymentIntentId, provider, total);
    }
    startPaymentPolling(intent.paymentIntentId);
  } catch (error) {
    console.error(error);
    publicPaymentStatus.textContent = 'Could not start payment. Please try again.';
  } finally {
    document.getElementById('publicPayBtn').disabled = false;
  }
}

async function launchManualUssd(paymentIntentId, provider, total) {
  const settings = getPaymentSettings();
  try {
    const response = await fetch(`${API_BASE}/payments/manual-instructions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paymentIntentId, provider, method: settings.method }),
    });
    const instructions = await response.json();
    if (!response.ok) throw new Error(instructions.error || 'Unavailable');

    if (instructions.callUrl) {
      window.location.href = instructions.callUrl;
      return;
    }
    if (instructions.smsUrl) {
      window.location.href = instructions.smsUrl;
      return;
    }
    if (instructions.webUrl) {
      window.location.href = instructions.webUrl;
      return;
    }
    if (instructions.appUrl) {
      const fallbackTimer = window.setTimeout(() => { window.location.href = MOBILE_MONEY_WEB[provider]; }, 1200);
      window.location.href = instructions.appUrl;
      window.setTimeout(() => window.clearTimeout(fallbackTimer), 1000);
      return;
    }
    if (instructions.ussdDialUrl) {
      publicPaymentStatus.textContent = `Opening ${provider === 'momo' ? 'MTN' : 'Airtel'} Money. Check the total, then enter your PIN.`;
      window.location.href = instructions.ussdDialUrl;
    }
  } catch (error) {
    console.error(error);
    publicPaymentStatus.textContent = `Could not open your mobile money app. Call support at ${SUPPORT_INQUIRY_NUMBER}.`;
  }
}

function startPaymentPolling(intentId) {
  let attempts = 0;
  const maxAttempts = 45; // ~3 minutes at 4s intervals
  clearInterval(paymentPollTimer);
  paymentPollTimer = setInterval(async () => {
    attempts += 1;
    try {
      const response = await fetch(`${API_BASE}/payments/intents/${encodeURIComponent(intentId)}/sync`, { method: 'POST' });
      const payment = await response.json();
      if (payment.status === 'paid' && payment.voucherCode) {
        clearInterval(paymentPollTimer);
        localStorage.removeItem(PAYMENT_INTENT_KEY);
        activateDashboard(payment.voucherCode, publicPhoneInput.value.trim(), payment.expiresAt);
        return;
      }
      if (payment.status === 'failed') {
        clearInterval(paymentPollTimer);
        publicPaymentStatus.textContent = 'Payment failed or was cancelled. Please try again.';
        return;
      }
      publicPaymentStatus.textContent = 'Waiting for you to confirm the payment on your phone...';
    } catch (error) {
      console.warn('Payment status unavailable', error);
    }
    if (attempts >= maxAttempts) {
      clearInterval(paymentPollTimer);
      publicPaymentStatus.textContent = 'Still waiting for confirmation. Use "Use voucher" once you receive your code.';
    }
  }, 4000);
}

async function checkPaymentIntent() {
  const intentId = localStorage.getItem(PAYMENT_INTENT_KEY);
  if (!intentId || IS_STATIC_DEPLOYMENT) return;
  try {
    const response = await fetch(`${API_BASE}/payments/intents/${encodeURIComponent(intentId)}`);
    if (!response.ok) return;
    const payment = await response.json();
    if (payment.status === 'paid' && payment.voucher_code) {
      localStorage.removeItem(PAYMENT_INTENT_KEY);
      activateDashboard(payment.voucher_code, publicPhoneInput?.value.trim() || '', payment.expires_at);
    } else {
      startPaymentPolling(intentId);
    }
  } catch (error) {
    console.warn('Payment status unavailable', error);
  }
}

function activateDashboard(voucherCode, phone, expiresAt) {
  localStorage.setItem(DASHBOARD_KEY, JSON.stringify({ voucherCode, phone }));
  voucherStatus.textContent = `Payment confirmed. Your WiFi voucher is ${voucherCode}.`;
  voucherInput.value = voucherCode;
  publicPaymentStatus.textContent = 'You are connected. See your remaining time below.';
  renderDashboard({ voucherCode, plan: publicPlan, expiresAt });
  loadStoredDashboard();
}

function renderDashboard(data) {
  if (!dashboardPanel) return;
  clearInterval(dashboardCountdownTimer);
  dashboardPanel.classList.remove('hidden');
  dashboardVoucherEl.textContent = data.voucherCode || '—';
  dashboardPlanEl.textContent = formatPlanName(data.plan || publicPlan);
  renderDashboardHistory(data.history || []);
  const expiresAtMs = data.expiresAt ? new Date(data.expiresAt).getTime() : 0;

  const tick = () => {
    const remainingMs = expiresAtMs - Date.now();
    if (remainingMs <= 0) {
      dashboardTimeEl.textContent = 'Expired';
      dashboardStatusEl.textContent = 'Your access has expired. Buy a new package to reconnect.';
      clearInterval(dashboardCountdownTimer);
      return;
    }
    const hours = Math.floor(remainingMs / 3600000);
    const minutes = Math.floor((remainingMs % 3600000) / 60000);
    const seconds = Math.floor((remainingMs % 60000) / 1000);
    dashboardTimeEl.textContent = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    dashboardStatusEl.textContent = 'Active';
  };
  tick();
  dashboardCountdownTimer = setInterval(tick, 1000);
}

async function loadStoredDashboard() {
  if (IS_STATIC_DEPLOYMENT) return;
  try {
    const stored = JSON.parse(localStorage.getItem(DASHBOARD_KEY) || 'null');
    if (!stored) return;
    const query = stored.voucherCode ? `voucher=${encodeURIComponent(stored.voucherCode)}` : `phone=${encodeURIComponent(stored.phone)}`;
    const response = await fetch(`${API_BASE}/dashboard?${query}`);
    if (!response.ok) return;
    const data = await response.json();
    if (data.isActive) {
      renderDashboard({ voucherCode: data.voucher_code, plan: data.plan, expiresAt: data.expires_at, history: data.history });
    } else {
      localStorage.removeItem(DASHBOARD_KEY);
    }
  } catch (error) {
    console.warn('Dashboard unavailable', error);
  }
}

function renderDashboardHistory(history) {
  const list = document.getElementById('dashboardHistoryList');
  if (!list) return;
  if (!history.length) {
    list.innerHTML = '';
    return;
  }
  list.innerHTML = history.map((record) => `
    <li>${formatPlanName(record.plan)} · ${formatCurrency(record.amount)} · ${record.status} · ${record.date}</li>
  `).join('');
}

function renewAccess() {
  clearInterval(dashboardCountdownTimer);
  dashboardPanel?.classList.add('hidden');
  publicPhoneInput?.focus();
}

async function loadSupportInfo() {
  const target = document.getElementById('supportInquiryNumber');
  if (!target) return;
  if (IS_STATIC_DEPLOYMENT) {
    target.textContent = SUPPORT_INQUIRY_NUMBER;
    return;
  }
  try {
    const response = await fetch(`${API_BASE}/support-info`);
    if (!response.ok) throw new Error('Unavailable');
    const data = await response.json();
    SUPPORT_INQUIRY_NUMBER = data.inquiryNumber || SUPPORT_INQUIRY_NUMBER;
  } catch (error) {
    console.warn('Support info unavailable', error);
  }
  target.textContent = SUPPORT_INQUIRY_NUMBER;
}


function getPaymentSettings() {
  try {
    return { ...DEFAULT_PAYMENT_SETTINGS, ...(JSON.parse(localStorage.getItem(PAYMENT_SETTINGS_KEY)) || {}) };
  } catch (error) {
    return DEFAULT_PAYMENT_SETTINGS;
  }
}

function loadPaymentSettings() {
  const settings = getPaymentSettings();
  paymentMethodSetting.value = settings.method;
}

function updatePublicAmount() {
  const planPrice = Math.round(PLAN_PRICES[publicPlan] * SPEED_TIER_MULTIPLIER[publicSpeedTier]);
  const serviceCharge = Math.ceil(planPrice * SERVICE_CHARGE_RATE);
  publicPlanTotalEl.textContent = formatCurrency(planPrice);
  publicServiceChargeEl.textContent = formatCurrency(serviceCharge);
  publicAmountDueEl.textContent = formatCurrency(planPrice + serviceCharge);
}

function renderTable() {
  const searchTerm = searchInput.value.trim().toLowerCase();
  const filteredRecords = records.filter((record) => {
    const query = `${record.customerName} ${record.phone} ${record.plan}`.toLowerCase();
    return query.includes(searchTerm);
  });

  if (!filteredRecords.length) {
    tableBody.innerHTML = '<tr><td colspan="5" class="empty-state">No client matches your search.</td></tr>';
    return;
  }

  tableBody.innerHTML = filteredRecords
    .slice()
    .reverse()
    .map(
      (record) => `
        <tr>
          <td><strong>${record.customerName}</strong><br><small>${record.phone}</small></td>
          <td>${formatPlanName(record.plan)}</td>
          <td>${isAdminAuthenticated ? formatCurrency(record.amount) : '🔒'}</td>
          <td><span class="status-badge ${record.status === 'paid' ? 'paid' : record.status === 'expired' ? 'expired' : 'pending'}">${record.status}</span></td>
          <td>
            <button class="action-btn pay" data-action="pay" data-id="${record.id}" ${record.status === 'expired' ? 'disabled' : ''}>${record.status === 'expired' ? 'Expired' : '💸 Pay'}</button>
            ${record.status === 'paid' ? `<button class="action-btn toggle" data-action="voucher" data-id="${record.id}">${record.voucherCode ? record.voucherCode : 'Create voucher'}</button>` : ''}
            <button class="action-btn toggle" data-action="toggle" data-id="${record.id}">Mark ${record.status === 'paid' ? 'pending' : 'paid'}</button>
            <button class="action-btn delete" data-action="delete" data-id="${record.id}">Delete</button>
          </td>
        </tr>
      `
    )
    .join('');
}

function resetForm() {
  form.reset();
  planTypeInput.value = 'monthly';
  deviceCountInput.value = 1;
  discountInput.value = 0;
  startDateInput.value = new Date().toISOString().slice(0, 10);
  document.querySelectorAll('.addon').forEach((box) => { box.checked = false; });
  calculateBill();
}

async function handleFormSubmit(event) {
  event.preventDefault();

  const bill = calculateBill();
  const customerName = customerNameInput.value.trim();
  const phone = customerPhoneInput.value.trim();
  if (!customerName || !phone) {
    alert('Please enter customer name and phone number.');
    return;
  }

  const record = {
    id: crypto.randomUUID(),
    customerName,
    phone,
    plan: planTypeInput.value,
    paymentMethod: paymentMethodInput.value,
    deviceCount: Number(deviceCountInput.value) || 1,
    date: safeDateValue(),
    amount: Math.max(0, Math.round(bill.total)),
    discount: bill.discount,
    status: 'pending',
    addons: bill.selectedAddons,
    routerId: routerOptionInput?.value || '',
  };

  const saved = await saveRecord(record);
  if (!saved) return;
  form.reset();
  resetForm();
  customerNameInput.focus();
}

async function handleTableClick(event) {
  const actionButton = event.target.closest('[data-action]');
  if (!actionButton) return;

  const { action, id } = actionButton.dataset;
  if (action === 'pay') {
    launchMobileMoney();
    return;
  }

  const target = records.find((record) => record.id === id);
  if (!target) return;

  if (action === 'toggle') {
    const updatedStatus = target.status === 'paid' ? 'pending' : 'paid';
    if (IS_STATIC_DEPLOYMENT) {
      target.status = updatedStatus;
      target.expiresAt = updatedStatus === 'paid'
        ? new Date(Date.now() + (PLAN_DURATIONS_MS[target.plan] || PLAN_DURATIONS_MS.monthly)).toISOString()
        : null;
      localStorage.setItem('wifiBillingRecords', JSON.stringify(records));
      renderAll();
      return;
    }
    try {
      await fetch(`${API_BASE}/clients/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: updatedStatus }),
      });
      await fetchRecords();
      return;
    } catch (error) {
      console.error(error);
      return;
    }
  }

  if (action === 'voucher') {
    const voucherCode = target.voucherCode || createVoucher(target);
    localStorage.setItem('wifiBillingRecords', JSON.stringify(records));
    alert(`Voucher: ${voucherCode}\nValid until: ${new Date(target.expiresAt).toLocaleString()}`);
    renderTable();
    return;
  }

  if (action === 'delete') {
    try {
      await fetch(`${API_BASE}/clients/${id}`, { method: 'DELETE' });
      await fetchRecords();
      return;
    } catch (error) {
      console.error(error);
      return;
    }
  }
}

function renderAll() {
  calculateBill();
  renderStats();
  renderReports();
  renderTable();
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = document.getElementById('adminUsername').value.trim();
  const password = document.getElementById('adminPassword').value.trim();
  if (IS_STATIC_DEPLOYMENT) {
    alert('Admin access is available only from the secure hosted website.');
    return;
  }

  try {
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) {
      alert(response.status === 401 ? 'Incorrect admin login details.' : 'The login service is unavailable.');
      return;
    }
    loginOverlay.classList.add('hidden');
    publicPortal.classList.add('hidden');
    adminApp.classList.remove('hidden');
    logoutBtn.classList.remove('hidden');
    isAdminAuthenticated = false;
    await fetchRecords();
    await fetchRouters();
  } catch (error) {
    console.error(error);
    alert('Could not connect to the billing service. Check your connection and try again.');
  }
});

logoutBtn.addEventListener('click', async () => {
  if (!IS_STATIC_DEPLOYMENT) await fetch(`${API_BASE}/auth/logout`, { method: 'POST' });
  loginOverlay.classList.remove('hidden');
  logoutBtn.classList.add('hidden');
  document.getElementById('adminPassword').value = '';
  isAdminAuthenticated = false;
  adminApp.classList.add('hidden');
  publicPortal.classList.remove('hidden');
});

adminAccessBtn.addEventListener('click', () => loginOverlay.classList.remove('hidden'));
cancelAdminBtn.addEventListener('click', () => loginOverlay.classList.add('hidden'));

document.querySelectorAll('[data-public-plan]').forEach((planButton) => {
  planButton.addEventListener('click', () => {
    publicPlan = planButton.dataset.publicPlan;
    document.querySelectorAll('[data-public-plan]').forEach((button) => button.classList.remove('selected'));
    planButton.classList.add('selected');
    updatePublicAmount();
  });
});

document.querySelectorAll('[data-speed-tier]').forEach((tierButton) => {
  tierButton.addEventListener('click', () => {
    publicSpeedTier = tierButton.dataset.speedTier;
    document.querySelectorAll('[data-speed-tier]').forEach((button) => button.classList.remove('selected'));
    tierButton.classList.add('selected');
    updatePublicAmount();
  });
});

document.getElementById('publicPayBtn').addEventListener('click', () => openPublicMobileMoney(publicProviderInput.value));
redeemVoucherBtn?.addEventListener('click', redeemVoucher);
dashboardRenewBtn?.addEventListener('click', renewAccess);

paymentSettingsForm.addEventListener('submit', (event) => {
  event.preventDefault();
  localStorage.setItem(PAYMENT_SETTINGS_KEY, JSON.stringify({ method: paymentMethodSetting.value }));
  paymentSettingsStatus.textContent = 'Payment settings saved on this admin device.';
});

protectedAccessForm?.addEventListener('submit', (event) => {
  event.preventDefault();
  const password = protectedPasswordInput.value.trim();
  if (password === ADMIN_PASSWORD) {
    isAdminAuthenticated = true;
    protectedInfoModal.classList.add('hidden');
    protectedPasswordInput.value = '';
    renderTable();
    renderStats();
  } else {
    alert('Incorrect password. Access denied.');
    protectedPasswordInput.value = '';
  }
});

addUserForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const name = document.getElementById('newUserName').value.trim();
  const password = document.getElementById('newUserPassword').value.trim();
  if (!name || !password) return;
  adminUsers.push(name);
  document.getElementById('newUserName').value = '';
  document.getElementById('newUserPassword').value = '';
  renderReports();
});

addRouterBtn?.addEventListener('click', addNewRouter);

document.querySelectorAll('.payment-option-btn').forEach((btn) => {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    const paymentMethod = e.target.dataset.payment;
    
    if (paymentMethod === 'momo' || paymentMethod === 'airtel') {
      openMobileMoneyPortal(paymentMethod);
    }
  });
});

document.getElementById('sendPaymentDetailsBtn')?.addEventListener('click', () => {
  sendPaymentDetails('Mobile Money', calculateBill().total);
});

connectWiFiBtn?.addEventListener('click', () => {
  if (wifiNetworks.length > 0) {
    const network = wifiNetworks[0];
    alert(`🔗 Connecting to "${network.name}"...\n\nSignal: ${network.signal}%\nSpeed: ${network.speed}\nSecurity: ${network.security}`);
  }
});

refreshBtn.addEventListener('click', fetchRecords);
printInvoiceBtn.addEventListener('click', () => window.print());
form.addEventListener('submit', handleFormSubmit);
searchInput.addEventListener('input', renderTable);
resetBtn.addEventListener('click', resetForm);
document.querySelectorAll('[data-pay-trigger]').forEach((trigger) => {
  trigger.addEventListener('click', launchMobileMoney);
});
document.addEventListener('click', handleTableClick);
[planTypeInput, deviceCountInput, discountInput].forEach((element) => {
  element.addEventListener('input', () => {
    if (!isAdminAuthenticated) {
      protectedInfoModal.classList.remove('hidden');
    }
    calculateBill();
  });
  element.addEventListener('change', () => {
    if (!isAdminAuthenticated) {
      protectedInfoModal.classList.remove('hidden');
    }
    calculateBill();
  });
});
document.querySelectorAll('.addon').forEach((checkbox) => {
  checkbox.addEventListener('change', calculateBill);
});

startDateInput.value = new Date().toISOString().slice(0, 10);
loadPaymentSettings();
updatePublicAmount();
loadSupportInfo();
if (IS_STATIC_DEPLOYMENT) {
  fetchRecords();
} else {
  calculateBill();
  restoreAdminSession();
  checkPaymentIntent();
  loadStoredDashboard();
}
window.setInterval(() => {
  expireRecords();
  renderTable();
}, 60 * 1000);

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js').catch((error) => console.warn('Offline cache unavailable', error));
}

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  installAppBtn?.classList.remove('hidden');
  publicInstallAppBtn?.classList.remove('hidden');
});

async function installApp() {
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
  installAppBtn.classList.add('hidden');
  publicInstallAppBtn?.classList.add('hidden');
}

installAppBtn?.addEventListener('click', installApp);
publicInstallAppBtn?.addEventListener('click', installApp);
