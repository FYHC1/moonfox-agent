import { el } from './dom.js';
import { generationParametersSchema, isValid } from './protocol.js';

const labels = {
  temperature: '温度 · temperature', top_p: '核采样 · top_p', max_tokens: '单次输出上限 · max_tokens',
  frequency_penalty: '频率惩罚 · frequency_penalty', presence_penalty: '存在惩罚 · presence_penalty', seed: '随机种子 · seed',
};
const openaiOnly = ['frequency_penalty', 'presence_penalty', 'seed'];

export function emptyGeneration() {
  return Object.fromEntries(Object.keys(generationParametersSchema.properties).map((key) => [key, null]));
}

export const nullableNumber = (value) => value.trim() === '' ? null : Number(value);
export const stopParameters = (text, clear = false) => clear ? [] : text === '' ? null : text.split(/\r?\n/);

export function generationError(values, protocol) {
  if (!isValid('GenerationParameters', values)) return '生成参数超出允许范围，请检查数字与停止词（最多 4 行）。';
  if (values.stop?.some((value) => !value || [...value].length > 200)) return '停止词每行须为 1–200 个字符，不能包含空行。';
  if (protocol === 'anthropic') {
    if (values.temperature != null && values.temperature > 1) return 'Anthropic 的温度不能超过 1。';
    if (values.temperature != null && values.top_p != null) return 'Anthropic 只能设置温度或 top_p 之一，另一个请留空。';
    if (openaiOnly.some((key) => values[key] != null)) return 'Anthropic 暂不支持惩罚参数与随机种子。';
  }
  return '';
}

// Only unsupported fields are cleared on an explicit protocol change, never on initial render.
export function generationForProtocol(values, protocol) {
  return protocol === 'anthropic' ? { ...values, frequency_penalty: null, presence_penalty: null, seed: null } : { ...values };
}

export function generationEditor({ value, prefix, disabled = false, protocol, modelDefaults = false, onChange }) {
  let current = { ...emptyGeneration(), ...value };
  let currentProtocol = protocol;
  const controls = new Map();
  const error = el('p', { class: 'error-text', role: 'status' });
  const protocolHint = el('p', { class: 'field-hint' });
  const update = (key, next) => { current = { ...current, [key]: next }; refresh(); onChange({ ...current }); };
  const numbers = el('div', { class: 'generation-fields' }, Object.entries(labels).map(([key, label]) => {
    const property = generationParametersSchema.properties[key];
    const schema = property.anyOf?.find((item) => item.type !== 'null') ?? property;
    const input = el('input', {
      id: `${prefix}-${key}`, type: 'number', step: schema.type === 'integer' ? 1 : 'any',
      min: schema.minimum ?? schema.exclusiveMinimum, max: schema.maximum,
      value: current[key] ?? '', disabled, placeholder: modelDefaults ? '协议默认' : '继承模型',
      onInput: (event) => update(key, nullableNumber(event.target.value)),
    });
    controls.set(key, input);
    return el('div', { class: 'field' }, el('label', { for: input.id }, label), input,
      key === 'max_tokens' && el('p', { class: 'field-hint' }, '单次模型输出限制，不是整个逻辑输入的 token 预算。'));
  }));
  const clear = el('input', { id: `${prefix}-clear-stop`, type: 'checkbox', disabled, checked: Array.isArray(current.stop) && current.stop.length === 0 });
  const stop = el('textarea', {
    id: `${prefix}-stop`, rows: 3, value: current.stop?.join('\n') ?? '', disabled,
    placeholder: '每行一个停止词，最多 4 行', spellcheck: 'false',
    onInput: (event) => update('stop', stopParameters(event.target.value, clear.checked)),
  });
  clear.addEventListener('change', () => { stop.value = ''; update('stop', clear.checked ? [] : null); });
  const element = el('div', { class: 'generation-editor' },
    el('p', { class: 'field-hint' }, modelDefaults
      ? '这是模型的生成默认值；留空使用协议默认。会话可单独覆盖，空值继承此处设置。'
      : '留空（null）继承模型默认值；填写即覆盖。只影响下一次运行，仍受执行预算限制。'),
    protocolHint, numbers,
    el('div', { class: 'field' }, el('label', { for: stop.id }, '停止词 · stop'), stop,
      el('p', { class: 'field-hint' }, modelDefaults ? '每行 1–200 字符；留空使用协议默认，勾选下方表示明确不设停止词。' : '每行 1–200 字符；空白输入表示继承，不会清除模型默认停止词。'),
      el('label', { class: 'check-option', for: clear.id }, clear, el('span', {}, el('strong', {}, '明确清空停止词（发送 []）')))), error);
  function refresh() {
    for (const [key, input] of controls) input.disabled = disabled || (currentProtocol === 'anthropic' && openaiOnly.includes(key));
    controls.get('temperature').max = currentProtocol === 'anthropic' ? 1 : generationParametersSchema.properties.temperature.anyOf.find((item) => item.type === 'number').maximum;
    stop.disabled = disabled || clear.checked;
    protocolHint.textContent = currentProtocol === 'anthropic' ? 'Anthropic：温度 ≤ 1，温度与 top_p 二选一；不支持惩罚、seed 或扩展思考。切换协议会清除不支持的字段。' : '';
    error.textContent = generationError(current, currentProtocol);
  }
  refresh();
  return {
    element,
    value: () => ({ ...current }),
    setProtocol(next) {
      currentProtocol = next;
      current = generationForProtocol(current, next);
      for (const [key, input] of controls) input.value = current[key] ?? '';
      refresh(); onChange({ ...current });
    },
  };
}
