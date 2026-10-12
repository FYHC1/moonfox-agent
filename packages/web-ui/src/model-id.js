// Model identity rules come from ONE source: the generated ManagedModelView contract.
// ModelSettings.provider_id is a provider rule and must never be reused for model IDs.
import { managedModelIdSchema } from './protocol.js';

export const modelIdMaxLength = managedModelIdSchema.maxLength;
export const modelIdPattern = managedModelIdSchema.pattern;

// The generated pattern is a JSON Schema regex. Compile it with the `u` flag (code points) and use
// setCustomValidity instead of an HTML `pattern` attribute, whose `v` flag may reject the charset.
const modelIdRegExp = new RegExp(modelIdPattern, 'u');

export const isModelId = (value) => typeof value === 'string'
  && [...value].length >= managedModelIdSchema.minLength && [...value].length <= modelIdMaxLength
  && modelIdRegExp.exec(value)?.[0] === value;

export function modelIdError(value) {
  if (typeof value !== 'string' || value === '') return '请填写模型 ID。';
  if ([...value].length > modelIdMaxLength) return `模型 ID 最多 ${modelIdMaxLength} 个字符（按 Unicode 码点计）。`;
  if (!isModelId(value)) return '模型 ID 不能以 . 开头，且不能包含空白、/ \\ ? # % 或控制字符。';
  return '';
}

// Mirrors the server rule: trim, collapse unsafe runs into '-', drop leading dots, default 'model',
// keep case/Unicode and cap at 200 code points.
const unsafeRuns = /[/\\?#%\s\x00-\x1f\x7f]+/gu;

export function modelIdBase(name) {
  const value = String(name ?? '').trim().replace(unsafeRuns, '-').replace(/^\.+/, '');
  return [...(value || 'model')].slice(0, modelIdMaxLength).join('');
}

// Front-end preview only; the server resolves the final identity and may append a different suffix.
export function autoModelId({ name, providerId = '', occupied = [] } = {}) {
  const taken = occupied instanceof Set ? occupied : new Set(occupied);
  const base = modelIdBase(name);
  if (!taken.has(base)) return base;
  const stem = [...`${providerId}-${base}`].slice(0, modelIdMaxLength).join('');
  let identity = stem;
  for (let suffix = 2; taken.has(identity); suffix += 1) {
    const ending = `-${suffix}`;
    identity = [...stem].slice(0, modelIdMaxLength - ending.length).join('') + ending;
  }
  return identity;
}
