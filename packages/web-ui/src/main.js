import { ApiClient, normalizeOrigin } from './client.js';
import { connectionScreen } from './login.js';
import { Workspace } from './workspace.js';

const root = document.getElementById('app');
let workspace = null;
let origin = ['http:', 'https:'].includes(location.protocol) ? location.origin : 'http://localhost:8765';
let connecting = false;

function showConnection(message) {
  workspace?.dispose();
  workspace = null;
  root.replaceChildren(connectionScreen({ origin, message, onConnect: connect }));
  document.querySelector('.skip-link').href = '#backend-origin';
}

async function connect(address, token) {
  if (connecting) return;
  connecting = true;
  let api;
  try {
    origin = normalizeOrigin(address);
    if (!token.trim()) throw new Error('请输入管理员令牌。');
    api = new ApiClient({ origin, token });
    const identity = await api.me();
    const sessions = await api.sessions();
    workspace = new Workspace({ api, identity, sessions, onDisconnect: showConnection });
    workspace.mount(root);
    document.querySelector('.skip-link').href = '#composer-input';
  } catch (error) {
    api?.clearCredentials();
    throw error;
  } finally { connecting = false; }
}

window.addEventListener('pageshow', (event) => {
  if (event.persisted) showConnection('页面已从后台恢复。为保护凭据，请重新验证连接。');
});

showConnection();
