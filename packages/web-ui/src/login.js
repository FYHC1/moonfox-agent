import { el, icon, button, logo, note } from './dom.js';

export function connectionScreen({ origin, message, onConnect }) {
  const address = el('input', {
    id: 'backend-origin', name: 'backend-origin', type: 'url', required: true,
    value: origin, placeholder: 'http://localhost:8765', autocomplete: 'url', spellcheck: 'false',
    'aria-describedby': 'origin-help',
  });
  const token = el('input', {
    id: 'admin-token', type: 'password', required: true, maxlength: 4096,
    placeholder: '输入宿主提供的管理员令牌', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'off', 'aria-describedby': 'token-help',
  });
  const feedback = el('div', { class: 'login-feedback' }, message && note(message, 'warning'));
  const submit = button('连接工作台', { type: 'submit', variant: 'primary', icon: 'arrow', className: 'connect-button' });
  const form = el('form', { class: 'connection-form', autocomplete: 'off' },
    el('div', { class: 'field' }, el('label', { for: 'backend-origin' }, '后端地址'),
      el('div', { class: 'input-with-icon' }, icon('globe'), address),
      el('p', { id: 'origin-help', class: 'field-hint' }, '默认连接当前站点；开发模式通过 Vite 代理访问本机后端。')),
    el('div', { class: 'field' }, el('label', { for: 'admin-token' }, '管理员令牌'),
      el('div', { class: 'input-with-icon' }, icon('lock'), token),
      el('p', { id: 'token-help', class: 'field-hint' }, '仅在此页面内存中使用，刷新后需重新输入。')),
    feedback, submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity() || submit.disabled) return;
    submit.disabled = true;
    submit.querySelector('span').textContent = '正在验证连接…';
    form.setAttribute('aria-busy', 'true');
    address.disabled = token.disabled = true;
    feedback.replaceChildren(note('正在验证身份并读取可访问的会话。'));
    const secret = token.value;
    token.value = '';
    try {
      await onConnect(address.value, secret);
    } catch (error) {
      feedback.replaceChildren(note(error.message, 'error'));
      token.focus();
    } finally {
      if (form.isConnected) {
        submit.disabled = address.disabled = token.disabled = false;
        submit.querySelector('span').textContent = '重新连接';
        form.setAttribute('aria-busy', 'false');
        token.focus();
      }
    }
  });

  return el('div', { class: 'connection-page' },
    el('header', { class: 'connection-header' },
      el('a', { class: 'brand', href: '#', 'aria-label': 'Moonfox 工作台首页' }, logo(40),
        el('span', { class: 'brand-copy' }, el('strong', {}, 'Moonfox'), el('span', {}, '个人 Agent 工作台'))),
      el('span', { class: 'protocol-label' }, el('span', { class: 'status-dot' }), 'STAGE D · PROTOCOL 01')),
    el('main', { class: 'connection-main' },
      el('section', { class: 'connection-intro', 'aria-labelledby': 'welcome-title' },
        el('div', { class: 'moon-scene', 'aria-hidden': true }, el('div', { class: 'moon-orbit orbit-outer' }),
          el('div', { class: 'moon-orbit orbit-inner' }), el('div', { class: 'moon-crescent' }),
          el('span', { class: 'orbit-point point-one' }), el('span', { class: 'orbit-point point-two' }),
          el('div', { class: 'scene-label' }, el('span', {}, 'YOUR SPACE, YOUR PACE'), el('span', {}, '↗'))),
        el('p', { class: 'eyebrow' }, '一个安静、可控的工作空间'),
        el('h1', { id: 'welcome-title' }, '从一个想法，', el('br'), el('span', {}, '到清晰的下一步。')),
        el('p', { class: 'intro-description' }, '与你的个人 Agent 对话，观察每次工具调用，', el('br'), '把执行的边界，留在自己手中。'),
        el('div', { class: 'intro-footnote' }, icon('shield', 16), '连接由你掌控 · 任务由服务端持续管理')),
      el('section', { class: 'connection-card', 'aria-labelledby': 'connection-title' },
        el('div', { class: 'card-kicker' }, el('span', { class: 'step-number' }, '01'), '建立安全连接'),
        el('h2', { id: 'connection-title' }, '欢迎回到 Moonfox'),
        el('p', { class: 'muted' }, '连接你的后端，继续未完的思路。'), form,
        el('div', { class: 'demo-disclosure' }, el('span', { class: 'demo-mark' }, 'DEMO'),
          el('div', {}, el('strong', {}, '离线演示为可选模式'), el('p', {}, '登录后可管理提供商与模型。启用 --demo 时使用预设回复，不调用真实 AI；能力以宿主配置为准。'))))),
    el('footer', { class: 'connection-footer' }, el('span', {}, 'MOONFOX / PERSONAL AGENT WORKSPACE'),
      el('span', {}, '关闭页面不会停止服务端任务')));
}
