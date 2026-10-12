import Ajv2020 from 'ajv/dist/2020.js';
import protocol from './generated/protocol.json' with { type: 'json' };

// Each exported DTO is a complete schema, with its own $defs namespace.
const validators = new Map();
export const protocolVersion = protocol.protocol_version;
export const sessionSettingsSchema = protocol.schemas.UpdateConfig.$defs.SessionSettings;

export class ProtocolError extends Error {
  constructor(schema, errors = []) {
    super(`服务端协议不匹配（${schema}），请确认前后端版本一致。`);
    this.name = 'ProtocolError';
    this.kind = 'protocol';
    this.schema = schema;
    // Paths/keywords only: never retain request values, credentials or raw provider data.
    this.issues = errors.map(({ instancePath, keyword }) => ({ instancePath, keyword }));
  }
}

function validator(name) {
  if (!validators.has(name)) {
    const schema = name === 'SessionSettings'
      ? { $ref: '#/$defs/SessionSettings', $defs: protocol.schemas.UpdateConfig.$defs }
      : protocol.schemas[name];
    if (!schema) throw new ProtocolError(name);
    const ajv = new Ajv2020({ allErrors: true, strict: false, validateFormats: false });
    validators.set(name, ajv.compile(schema));
  }
  return validators.get(name);
}

export function isValid(name, value) {
  return Boolean(validator(name)(value));
}

export function assertContract(name, value) {
  const check = validator(name);
  if (!check(value)) throw new ProtocolError(name, check.errors);
  return value;
}

export function assertEvent(event) {
  assertContract('WireEvent', event);
  const { type, payload } = event;
  if (type === 'message_start' || type === 'message_end') assertContract('MessageView', payload.message);
  if (type === 'input_received' || type === 'run_started' || type === 'run_finished') {
    assertContract('InputView', payload.input);
    if (type !== 'input_received' && typeof payload.status !== 'string') throw new ProtocolError('run status');
  }
  if (type === 'approval_requested' || type === 'approval_resolved') assertContract('ApprovalView', payload.approval);
  if (type === 'text_delta' && (typeof payload.message_id !== 'string' || typeof payload.delta !== 'string')) {
    throw new ProtocolError('text_delta');
  }
  // The backend owns the payload schema. Only public text is projected; partial/reasoning is never used.
  return event;
}
