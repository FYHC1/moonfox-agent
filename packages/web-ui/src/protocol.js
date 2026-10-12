import Ajv2020 from 'ajv/dist/2020.js';
import protocol from './generated/protocol.json' with { type: 'json' };

// Each exported DTO is a complete schema, with its own $defs namespace.
const validators = new Map();
export const protocolVersion = protocol.protocol_version;
const definitionSources = {
  SessionSettings: 'UpdateConfig',
  GenerationParameters: 'PutModel',
  ProviderSettings: 'PutProvider',
  ModelSettings: 'PutModel',
};
export const sessionSettingsSchema = protocol.schemas.UpdateConfig.$defs.SessionSettings;
export const providerSettingsSchema = protocol.schemas.PutProvider?.$defs.ProviderSettings;
export const modelSettingsSchema = protocol.schemas.PutModel?.$defs.ModelSettings;
export const generationParametersSchema = protocol.schemas.PutModel?.$defs.GenerationParameters
  ?? protocol.schemas.UpdateConfig.$defs.GenerationParameters;
// Model identity has a single source: the generated ManagedModelView contract. It is a URL path
// segment rule, deliberately different from ModelSettings.provider_id; never validate model IDs
// with the provider pattern.
export const managedModelIdSchema = protocol.schemas.ManagedModelView.properties.id;

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
    const source = protocol.schemas[definitionSources[name]];
    const schema = protocol.schemas[name] ?? (source?.$defs?.[name]
      ? { $ref: `#/$defs/${name}`, $defs: source.$defs } : null);
    if (!schema) throw new ProtocolError(name);
    const ajv = new Ajv2020({ allErrors: true, strict: false, strictNumbers: true, validateFormats: false });
    validators.set(name, ajv.compile(schema));
  }
  return validators.get(name);
}

export function isValid(name, value) {
  return Boolean(validator(name)(value));
}

const jsonSchemaValidators = new WeakMap();

// Extension schemas arrive in the host catalog, so they are validated at the point of use and
// never copied into the generated protocol bundle.
export function validateJsonSchema(schema, value) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return false;
  try {
    let check = jsonSchemaValidators.get(schema);
    if (!check) {
      check = new Ajv2020({ allErrors: true, strict: false, strictNumbers: true, validateFormats: false }).compile(schema);
      jsonSchemaValidators.set(schema, check);
    }
    return Boolean(check(value));
  } catch {
    return false;
  }
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
