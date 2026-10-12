export function text(value, doc = document) {
  return doc.createTextNode(value == null ? '' : String(value));
}

export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (['disabled', 'checked', 'selected', 'hidden', 'readOnly', 'required', 'multiple'].includes(key)) node[key] = value;
    else if (key === 'value') node.value = value;
    else node.setAttribute(key, value === true && !key.startsWith('aria-') ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : text(child));
  }
  return node;
}

const paths = {
  plus: ['M12 5v14M5 12h14'],
  arrow: ['M12 19V5m-6 6 6-6 6 6'],
  chevron: ['m9 5 7 7-7 7'],
  down: ['m6 9 6 6 6-6'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  close: ['m6 6 12 12M6 18 18 6'],
  panel: ['M9 4v16', 'M4 4h16v16H4z'],
  settings: ['M4 7h16M4 17h16M8 4v6M16 14v6'],
  chat: ['M4 5h16v12H9l-5 4V5z'],
  check: ['m5 12 4 4L19 6'],
  stop: ['M6 6h12v12H6z'],
  retry: ['M20 7v5h-5', 'M19 12a7 7 0 1 0-2 5'],
  plug: ['M8 3v5m8-5v5M6 8h12v2a6 6 0 0 1-6 6v5M6 4l14 16'],
  lock: ['M6 10h12v10H6z', 'M8 10V7a4 4 0 0 1 8 0v3M12 14v2'],
  eye: ['M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6'],
  globe: ['M3 12h18M12 3c-5 5-5 13 0 18 5-5 5-13 0-18', 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18'],
  tool: ['m14 6 4-4a6 6 0 0 0-7 7l-8 8a2 2 0 0 0 4 4l8-8a6 6 0 0 0 7-7l-4 4-4-4z'],
  cube: ['m12 3 9 5v9l-9 5-9-5V8l9-5zM3 8l9 5 9-5M12 13v9M7 5l9 5'],
  info: ['M12 11v6M12 7v1', 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20'],
  clock: ['M12 6v6l4 2', 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18'],
  alert: ['m12 3 10 18H2L12 3zM12 9v5M12 17v1'],
  search: ['M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6'],
  link: ['m10 13 4-4M8 15l-1 1a3 3 0 1 1-4-4l4-4a3 3 0 0 1 4 0m2 1 1-1a3 3 0 1 1 4 4l-4 4a3 3 0 0 1-4 0'],
  shield: ['m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3z', 'm8 12 3 3 5-6'],
  enter: ['M19 4v10H5m5-5-5 5 5 5'],
};

export function icon(name, size = 18) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' })) {
    svg.setAttribute(key, value);
  }
  for (const d of paths[name] ?? paths.info) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

export function button(label, { icon: iconName, variant = 'secondary', className = '', ...attributes } = {}) {
  return el('button', { type: 'button', class: `button button-${variant} ${className}`.trim(), ...attributes }, iconName && icon(iconName), el('span', {}, label));
}

export function iconButton(label, iconName, attributes = {}) {
  return el('button', { type: 'button', class: 'icon-button', title: label, 'aria-label': label, ...attributes }, icon(iconName));
}

export function badge(label, tone = 'neutral') {
  return el('span', { class: `badge badge-${tone}` }, el('span', { class: 'status-dot', 'aria-hidden': true }), label);
}

export function note(message, tone = 'info', action) {
  return el('div', { class: `notice notice-${tone}`, role: tone === 'error' ? 'alert' : 'status' },
    icon(tone === 'error' || tone === 'warning' ? 'alert' : tone === 'success' ? 'check' : 'info', 16),
    el('div', { class: 'notice-body' }, el('span', {}, message), action));
}

const statusLabels = {
  idle: '空闲', running: '运行中', pending: '待处理', queued: '排队中', received: '已接收', accepted: '已接收',
  completed: '已完成', complete: '已完成', aborted: '已停止', cancelled: '已取消', canceled: '已取消',
  error: '运行出错', limit_reached: '达到预算上限', unknown: '状态待核实',
  approved: '已批准', denied: '已拒绝', expired: '已过期', waiting_approval: '等待审批',
};
export const statusLabel = (status) => statusLabels[status] ?? status ?? '未知';
export const statusTone = (status) => ['error', 'denied'].includes(status) ? 'danger'
  : ['limit_reached', 'pending', 'queued', 'unknown', 'waiting_approval'].includes(status) ? 'warning'
    : ['running', 'completed', 'complete', 'approved'].includes(status) ? 'success' : 'neutral';
export const asMillis = (timestamp) => timestamp > 1e12 ? timestamp : timestamp * 1000;
export const timeLabel = (timestamp) => timestamp ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(asMillis(timestamp)) : '';
export const countLabel = (value) => new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value ?? 0);
export const modelLabel = (id) => id === 'demo' ? 'Demo · 离线演示' : id;
export const logo = (size = 36) => el('img', { class: 'fox-logo', src: `${import.meta.env?.BASE_URL ?? './'}moonfox.svg`, width: size, height: size, alt: '' });

export function announce(message) {
  const region = document.getElementById('live-region');
  if (region) region.textContent = message;
}

export function confirmAction({ title, description, confirmLabel, danger = false }) {
  return new Promise((resolve) => {
    const dialog = el('dialog', { class: 'confirm-dialog', 'aria-labelledby': 'confirm-title', 'aria-describedby': 'confirm-description' },
      el('h2', { id: 'confirm-title' }, title), el('p', { id: 'confirm-description' }, description));
    const finish = (answer) => { dialog.close(); dialog.remove(); resolve(answer); };
    dialog.append(el('div', { class: 'dialog-actions' }, button('返回', { onClick: () => finish(false), autofocus: true }),
      button(confirmLabel, { variant: danger ? 'danger' : 'primary', onClick: () => finish(true) })));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(false); });
    document.body.append(dialog);
    dialog.showModal();
  });
}
