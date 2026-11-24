// Precarga del CLI legado: mismo contrato de configuración privada que Python.
const fs = require('node:fs');
if (process.env.SERVICE_ENV_FILE) {
  const values = JSON.parse(fs.readFileSync(process.env.SERVICE_ENV_FILE, 'utf8'));
  for (const [name, value] of Object.entries(values)) {
    if (!/^[A-Z][A-Z0-9_]*$/.test(name) || typeof value !== 'string') {
      throw new Error('Configuración privada inválida');
    }
    if (!process.env[name]) process.env[name] = value;
  }
}
