import { el, note } from './dom.js';
import { validateJsonSchema } from './protocol.js';

const clone = (value) => structuredClone(value);
const isObject = (value) => Boolean(value && typeof value === 'object' && !Array.isArray(value));
const labels = { persona: '人设与对话示例', memory: '记忆配置' };

export function setExtensionConfig(settings, extensionId, value) {
  const configJson = JSON.stringify(value);
  if (typeof configJson !== 'string') throw new TypeError('Extension config must be JSON-serializable.');
  const source = Array.isArray(settings?.extension_config) ? settings.extension_config : [];
  let found = false;
  const entries = source.map((entry) => {
    if (!entry || entry.extension_id !== extensionId) return clone(entry);
    found = true;
    return { ...clone(entry), extension_id: extensionId, config_json: configJson };
  });
  if (!found) entries.push({ extension_id: extensionId, config_json: configJson });
  return entries;
}

function schemaDefaults(schema) {
  return Object.fromEntries(Object.entries(schema?.properties ?? {})
    .filter(([, property]) => Object.hasOwn(property ?? {}, 'default'))
    .map(([key, property]) => [key, clone(property.default)]));
}

function initialConfig(extension, settings) {
  const stored = (Array.isArray(settings?.extension_config) ? settings.extension_config : [])
    .find((entry) => entry?.extension_id === extension.id);
  const source = typeof stored?.config_json === 'string' ? stored.config_json
    : typeof extension.default_config_json === 'string' ? extension.default_config_json
      : JSON.stringify(schemaDefaults(extension.config_schema));
  try {
    const parsed = JSON.parse(source);
    return isObject(parsed)
      ? { value: parsed, source, error: '' }
      : { value: null, source, error: '扩展配置必须是 JSON 对象。' };
  } catch {
    return { value: null, source, error: '扩展提供的配置不是有效 JSON；修复前不会覆盖现有内容。' };
  }
}

function field(label, id, control, hint) {
  return el('div', { class: 'field' }, el('label', { for: id }, label), control, hint && el('p', { class: 'field-hint' }, hint));
}

function schemaProp(schema, key) { return schema?.properties?.[key] ?? {}; }
function displayed(value, schema, key, fallback = '') {
  if (Object.hasOwn(value ?? {}, key)) return value[key];
  return Object.hasOwn(schemaProp(schema, key), 'default') ? schemaProp(schema, key).default : fallback;
}

export function extensionConfigEditor(extension, settings, { disabled = false, onChange = () => {}, onValidityChange = () => {} } = {}) {
  const schema = extension.config_schema;
  if (!isObject(schema)) return null;
  const id = extension.id;
  const title = labels[id] ?? `扩展设置 · ${id}`;
  const init = initialConfig(extension, settings);
  let value = init.value;
  let parseError = init.error;
  const validity = el('p', { class: 'error-text extension-config-error', role: 'status' });
  const content = el('div', { class: 'extension-config-content' });
  const detail = el('details', { class: 'extension-config-card', open: ['persona', 'memory'].includes(id) },
    el('summary', { class: 'extension-config-summary' }, el('strong', {}, title), el('code', {}, `v${extension.version ?? '—'}`)), content);

  const persist = (next) => {
    value = next;
    parseError = '';
    onChange('extension_config', setExtensionConfig(settings, id, value));
    refreshValidity();
    onValidityChange();
  };
  function refreshValidity() {
    const message = parseError || (value && !validateJsonSchema(schema, value)
      ? '当前 JSON 不符合扩展声明的配置 schema；请修正字段与取值后再保存。' : '');
    validity.textContent = message;
    return !message;
  }
  const setField = (key, nextValue) => persist({ ...value, [key]: nextValue });
  const editable = !disabled && Boolean(value);

  if (extension.enabled === false || !(settings.enabled_extensions ?? []).includes(id)) {
    content.append(note('扩展当前未启用。编辑配置不会启用它；已保存的数据会保留。', 'warning'));
  }
  if (!value) {
    const raw = el('textarea', { id: `extension-config-${id}-raw`, rows: 8, maxlength: 32768, value: init.source, disabled,
      'aria-label': `${id} 配置 JSON`, onInput: (event) => {
        const source = event.target.value;
        try {
          const parsed = JSON.parse(source);
          if (!isObject(parsed)) throw new TypeError('Expected object');
          value = parsed;
          parseError = '';
          onChange('extension_config', setExtensionConfig(settings, id, value));
        } catch {
          parseError = '请输入有效的 JSON 对象。';
        }
        refreshValidity(); onValidityChange();
      },
    });
    content.append(field('完整配置 JSON', raw.id, raw, '保留所有字段；保存前会按宿主提供的 schema 校验。'));
  } else if (id === 'persona') {
    content.append(field('版本', `extension-${id}-version`, el('input', { id: `extension-${id}-version`, type: 'number', value: displayed(value, schema, 'version', 1), readOnly: true, disabled: true }),
      '人设配置 schema 版本；保存时不会自动升级。'));
    const textInput = (key, label, { multiline = false, hint = null } = {}) => {
      const property = schemaProp(schema, key);
      const inputId = `extension-${id}-${key}`;
      const attrs = { id: inputId, value: displayed(value, schema, key), maxlength: property.maxLength, disabled: !editable,
        onInput: (event) => setField(key, event.target.value) };
      const control = multiline
        ? el('textarea', { ...attrs, rows: key === 'identity' || key === 'style' ? 4 : 3 })
        : el('input', { ...attrs, type: 'text', required: property.minLength > 0 });
      return field(label, inputId, control, hint);
    };
    content.append(el('div', { class: 'extension-config-fields' },
      textInput('name', '人设名称'),
      textInput('profile_id', '配置档案 ID', { hint: '小写字母开头，仅允许小写字母、数字、下划线和连字符。' }),
      field('修订号', `extension-${id}-revision`, el('input', {
        id: `extension-${id}-revision`, type: 'number', step: 1, min: schemaProp(schema, 'revision').minimum ?? 1,
        max: schemaProp(schema, 'revision').maximum, value: displayed(value, schema, 'revision', 1), required: true, disabled: !editable,
        onInput: (event) => setField('revision', event.target.value === '' ? null : Number(event.target.value)),
      }), '用于标记静态人设版本；不会保存关系状态或聊天记忆。'),
      textInput('identity', '身份设定', { multiline: true }),
      textInput('style', '交流风格', { multiline: true }),
      (() => {
        const inputId = `extension-${id}-honesty_mode`;
        const property = schemaProp(schema, 'honesty_mode');
        const modes = { roleplay: '角色扮演（默认）', honest: '明确说明 AI 身份', deflect: '温和回避' };
        const control = el('select', { id: inputId, disabled: !editable, onChange: (event) => setField('honesty_mode', event.target.value) },
          (property.enum ?? ['roleplay', 'honest', 'deflect']).map((mode) => el('option', { value: mode, selected: displayed(value, schema, 'honesty_mode', 'roleplay') === mode }, modes[mode] ?? mode)));
        return field('认知档位', inputId, control, '只影响下一次运行，不会修改历史消息。');
      })(),
      (() => {
        const inputId = `extension-${id}-examples`;
        const examples = displayed(value, schema, 'examples', []);
        let exampleText = JSON.stringify(examples, null, 2);
        let exampleError = '';
        const control = el('textarea', { id: inputId, rows: 8, maxlength: 32768, value: exampleText, disabled: !editable,
          'aria-label': '对话示例 JSON', onInput: (event) => {
            exampleText = event.target.value;
            try {
              const parsed = JSON.parse(exampleText);
              if (!Array.isArray(parsed)) throw new TypeError('Expected array');
              exampleError = '';
              setField('examples', parsed);
            } catch {
              exampleError = '对话示例必须是 JSON 数组；修正前无法保存。';
              parseError = exampleError;
              refreshValidity(); onValidityChange();
            }
          },
        });
        const hint = el('div', {}, el('p', { class: 'field-hint' }, `最多 ${schemaProp(schema, 'examples').maxItems ?? 8} 条；每条包含 user 与 assistant 文本。示例不是历史对话。`),
          el('p', { class: 'error-text', role: 'status', 'data-examples-error': id }));
        control.addEventListener('input', () => { hint.querySelector('[data-examples-error]').textContent = exampleError; });
        return field('对话示例 JSON', inputId, control, hint);
      })()));
  } else if (id === 'memory') {
    const memoryFields = [];
    const recallId = `extension-${id}-recall`;
    memoryFields.push(field('召回记忆', recallId, el('label', { class: 'check-option', for: recallId },
      el('input', { id: recallId, type: 'checkbox', checked: displayed(value, schema, 'recall', true), disabled: !editable,
        onChange: (event) => setField('recall', event.target.checked) }), el('span', {}, el('strong', {}, '在后续运行中召回本会话记忆'))),
    '关闭只停止注入召回，不删除已保存的扩展副本。'));
    for (const [key, label] of [['max_results', '最多召回条数'], ['max_reference_tokens', '记忆引用 token 预算'], ['extraction_max_tokens', '抽取 token 上限']]) {
      const property = schemaProp(schema, key);
      const inputId = `extension-${id}-${key}`;
      memoryFields.push(field(label, inputId, el('input', { id: inputId, type: 'number', step: 1, min: property.minimum, max: property.maximum,
        value: displayed(value, schema, key), required: true, disabled: !editable,
        onInput: (event) => setField(key, event.target.value === '' ? null : Number(event.target.value)),
      }), `范围 ${property.minimum}–${property.maximum}。`));
    }
    const extractionId = `extension-${id}-extraction`;
    const extraction = el('select', { id: extractionId, disabled: !editable, onChange: (event) => { setField('extraction', event.target.value); extractionWarning.hidden = event.target.value !== 'model'; } },
      (schemaProp(schema, 'extraction').enum ?? ['off', 'model']).map((mode) => el('option', { value: mode, selected: displayed(value, schema, 'extraction', 'off') === mode }, mode === 'off' ? '关闭（默认）' : '使用模型抽取')));
    memoryFields.push(field('自动抽取', extractionId, extraction, '默认关闭；不会因启用扩展或保存其它字段而开启。'));
    const extractionWarning = note('模型抽取会额外请求模型，并与原输入共享请求与 token 预算。只有显式选择“使用模型抽取”才会启用。', 'warning');
    extractionWarning.hidden = displayed(value, schema, 'extraction', 'off') !== 'model';
    memoryFields.push(el('div', { class: 'management-wide' }, extractionWarning));
    content.append(el('div', { class: 'extension-config-fields' }, memoryFields));
  } else {
    const inputId = `extension-config-${id}-json`;
    const control = el('textarea', { id: inputId, rows: 10, maxlength: 32768, value: JSON.stringify(value, null, 2), disabled: !editable,
      'aria-label': `${id} 配置 JSON`, onInput: (event) => {
        const source = event.target.value;
        try {
          const parsed = JSON.parse(source);
          if (!isObject(parsed)) throw new TypeError('Expected object');
          value = parsed;
          parseError = '';
          onChange('extension_config', setExtensionConfig(settings, id, value));
        } catch {
          parseError = '请输入有效的 JSON 对象。';
        }
        refreshValidity(); onValidityChange();
      },
    });
    content.append(field('完整配置 JSON', inputId, control, '通用 schema 编辑器会保留并显示完整对象；未知字段不会被表单静默丢弃。'));
  }

  if (id === 'memory') content.append(note('设置只在保存后影响后续运行；管理记忆不会写入聊天输入。', 'info'));
  if (!disabled && !value && !parseError) parseError = '当前扩展配置不可编辑。';
  content.append(validity);
  refreshValidity();
  return { element: detail, validate: refreshValidity };
}
