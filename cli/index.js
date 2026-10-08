#!/usr/bin/env node
/**
 * cc-switch — CLI for CC Switch
 *
 * Switch Claude Code / Codex / Gemini / GrokBuild providers from the command line.
 * Reads and writes the CC Switch SQLite database (~/.cc-switch/cc-switch.db).
 *
 * Usage:
 *   npx cc-switch                     — show current provider for each app
 *   npx cc-switch list [--app claude]  — list all providers
 *   npx cc-switch use <name> [--app claude]  — switch to a provider
 *   npx cc-switch add <name> --url <url> --key <key> [--app claude]
 *   npx cc-switch rm <name> [--app claude]   — remove a provider
 *   npx cc-switch current [--app claude]     — show current provider details
 *   npx cc-switch health [--app claude]      — check provider health
 *   npx cc-switch proxy [--app claude]       — show proxy config
 *   npx cc-switch proxy on [--app claude]    — enable proxy
 *   npx cc-switch proxy off [--app claude]   — disable proxy
 */

import { execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync as _writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const _fs = { writeFileSync: _writeFileSync };

const DB_PATH = join(homedir(), ".cc-switch", "cc-switch.db");
const SETTINGS_PATH = join(homedir(), ".cc-switch", "settings.json");

const VALID_APPS = ["claude", "codex", "gemini", "grokbuild", "claude-desktop", "openclaw", "hermes"];
const DEFAULT_APPS = ["claude", "codex"];

// ─── SQLite helpers ────────────────────────────────────────────────

function sql(query, ...params) {
  if (!existsSync(DB_PATH)) {
    console.error(`CC Switch database not found: ${DB_PATH}`);
    console.error("Please install and run CC Switch desktop app first.");
    process.exit(1);
  }
  const args = [DB_PATH, "-json", "-header", query];
  if (params.length) {
    // Use parameterized queries via .param binding isn't trivial in CLI,
    // so we just escape for display queries (read-only).
  }
  try {
    const result = execSync(`sqlite3 ${args.map(a => `"${a}"`).join(" ")}`, {
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    });
    if (!result.trim()) return [];
    return JSON.parse(result);
  } catch (e) {
    return [];
  }
}

function sqlRun(query) {
  execSync(`sqlite3 "${DB_PATH}" "${query.replace(/"/g, '""')}"`, {
    encoding: "utf-8",
    maxBuffer: 10 * 1024 * 1024,
  });
}

function escSql(str) {
  return String(str).replace(/'/g, "''");
}

// ─── Data access ───────────────────────────────────────────────────

function getProviders(appType) {
  return sql(
    `SELECT id, app_type, name, category, is_current, provider_type FROM providers WHERE app_type='${escSql(appType)}' ORDER BY sort_index, name`
  );
}

function getProviderByName(appType, name) {
  const rows = sql(
    `SELECT id, app_type, name, settings_config, category, is_current FROM providers WHERE app_type='${escSql(appType)}' AND name='${escSql(name)}'`
  );
  return rows[0] || null;
}

function getCurrentProvider(appType) {
  const rows = sql(
    `SELECT id, app_type, name, settings_config, category, is_current FROM providers WHERE app_type='${escSql(appType)}' AND is_current=1`
  );
  return rows[0] || null;
}

function switchTo(appType, providerId) {
  // Unset all current for this app_type
  sqlRun(`UPDATE providers SET is_current=0 WHERE app_type='${escSql(appType)}'`);
  // Set new current
  sqlRun(`UPDATE providers SET is_current=1 WHERE id='${escSql(providerId)}' AND app_type='${escSql(appType)}'`);
}

function addProvider(appType, name, url, apiKey) {
  const id = crypto.randomUUID();
  const settingsConfig = JSON.stringify({
    env: {
      ANTHROPIC_BASE_URL: url,
      ANTHROPIC_API_KEY: apiKey,
      ANTHROPIC_AUTH_TOKEN: apiKey,
    },
  });
  sqlRun(
    `INSERT INTO providers (id, app_type, name, settings_config, category, is_current, sort_index, meta) VALUES ('${escSql(id)}', '${escSql(appType)}', '${escSql(name)}', '${escSql(settingsConfig)}', 'custom', 0, 99, '{}')`
  );
  return id;
}

function removeProvider(appType, name) {
  sqlRun(
    `DELETE FROM providers WHERE app_type='${escSql(appType)}' AND name='${escSql(name)}' AND category!='official'`
  );
}

function getProxyConfig(appType) {
  const rows = sql(`SELECT * FROM proxy_config WHERE app_type='${escSql(appType)}'`);
  return rows[0] || null;
}

function setProxyEnabled(appType, enabled) {
  sqlRun(
    `UPDATE proxy_config SET proxy_enabled=${enabled ? 1 : 0}, enabled=${enabled ? 1 : 0} WHERE app_type='${escSql(appType)}'`
  );
}

function getHealth(appType) {
  return sql(
    `SELECT provider_id, is_healthy, consecutive_failures, last_success_at, last_failure_at, last_error FROM provider_health WHERE app_type='${escSql(appType)}'`
  );
}

// ─── Display helpers ───────────────────────────────────────────────

function dim(s) { return `\x1b[2m${s}\x1b[0m`; }
function bold(s) { return `\x1b[1m${s}\x1b[0m`; }
function green(s) { return `\x1b[32m${s}\x1b[0m`; }
function yellow(s) { return `\x1b[33m${s}\x1b[0m`; }
function red(s) { return `\x1b[31m${s}\x1b[0m`; }
function cyan(s) { return `\x1b[36m${s}\x1b[0m`; }

function parseConfig(configStr) {
  try {
    return JSON.parse(configStr || "{}");
  } catch {
    return {};
  }
}

function maskKey(key) {
  if (!key || key.length < 8) return key || "";
  return key.slice(0, 4) + "…" + key.slice(-4);
}

// ─── Commands ──────────────────────────────────────────────────────

function cmdDefault(apps) {
  // Show current provider for each app
  for (const app of apps) {
    const cur = getCurrentProvider(app);
    if (cur) {
      console.log(`  ${bold(app)}: ${green(cur.name)} ${dim(`[${cur.category}]`)} ${cur.is_current ? green("●") : ""}`);
    } else {
      console.log(`  ${bold(app)}: ${dim("no provider selected")}`);
    }
  }
}

function cmdList(appType) {
  const providers = getProviders(appType);
  if (!providers.length) {
    console.log(`No providers for ${appType}`);
    return;
  }
  console.log(`\n${bold(appType)} providers:\n`);
  for (const p of providers) {
    const marker = p.is_current ? green("●") : " ";
    const cat = dim(`[${p.category}]`);
    console.log(`  ${marker} ${p.name.padEnd(30)} ${cat}`);
  }
  console.log();
}

function cmdUse(appType, name) {
  const provider = getProviderByName(appType, name);
  if (!provider) {
    console.error(`Provider "${name}" not found for ${appType}`);
    console.error(`Run "${bold("cc-switch list --app " + appType)}" to see available providers.`);
    process.exit(1);
  }
  switchTo(appType, provider.id);

  // Also update settings.json currentProvider
  try {
    const { writeFileSync } = _fs;
    const settings = JSON.parse(readFileSync(SETTINGS_PATH, "utf-8"));
    const key = `currentProvider${appType.charAt(0).toUpperCase() + appType.slice(1).replace(/-(.)/g, (_, c) => c.toUpperCase())}`;
    settings[key] = provider.id;
    writeFileSync(SETTINGS_PATH, JSON.stringify(settings, null, 2) + "\n", "utf-8");
  } catch {}

  console.log(`Switched ${appType} → ${green(name)}`);
}

function cmdCurrent(appType) {
  const cur = getCurrentProvider(appType);
  if (!cur) {
    console.log(`No current provider for ${appType}`);
    return;
  }
  const config = parseConfig(cur.settings_config);
  const env = config.env || {};

  console.log(`\n${bold(appType)} current provider: ${green(cur.name)} ${dim(`[${cur.category}]`)}\n`);
  if (env.ANTHROPIC_BASE_URL) console.log(`  URL:   ${cyan(env.ANTHROPIC_BASE_URL)}`);
  if (env.ANTHROPIC_API_KEY) console.log(`  Key:   ${maskKey(env.ANTHROPIC_API_KEY)}`);
  if (env.ANTHROPIC_MODEL) console.log(`  Model: ${env.ANTHROPIC_MODEL}`);
  if (config.model) console.log(`  Slot:  ${config.model}`);
  console.log();
}

function cmdAdd(appType, name, url, apiKey) {
  const existing = getProviderByName(appType, name);
  if (existing) {
    console.error(`Provider "${name}" already exists for ${appType}`);
    process.exit(1);
  }
  addProvider(appType, name, url, apiKey);
  console.log(`Added ${appType} provider: ${green(name)} → ${cyan(url)}`);
}

function cmdRm(appType, name) {
  const provider = getProviderByName(appType, name);
  if (!provider) {
    console.error(`Provider "${name}" not found for ${appType}`);
    process.exit(1);
  }
  if (provider.category === "official") {
    console.error(`Cannot remove official provider "${name}"`);
    process.exit(1);
  }
  removeProvider(appType, name);
  console.log(`Removed ${appType} provider: ${name}`);
}

function cmdProxy(appType, action) {
  const config = getProxyConfig(appType);
  if (!config) {
    console.log(`No proxy config for ${appType}`);
    return;
  }
  if (action === "on") {
    setProxyEnabled(appType, true);
    console.log(`Proxy ${green("enabled")} for ${appType} (port ${config.listen_port})`);
  } else if (action === "off") {
    setProxyEnabled(appType, false);
    console.log(`Proxy ${red("disabled")} for ${appType}`);
  } else {
    const status = config.proxy_enabled ? green("on") : red("off");
    console.log(`\n${bold(appType)} proxy: ${status}`);
    console.log(`  Listen:     ${config.listen_address}:${config.listen_port}`);
    console.log(`  Logging:    ${config.enable_logging ? "on" : "off"}`);
    console.log(`  Failover:   ${config.auto_failover_enabled ? "on" : "off"}`);
    console.log(`  Max retries: ${config.max_retries}`);
    console.log();
  }
}

function cmdHealth(appType) {
  const health = getHealth(appType);
  if (!health.length) {
    console.log(`No health data for ${appType}`);
    return;
  }
  console.log(`\n${bold(appType)} provider health:\n`);
  for (const h of health) {
    const provider = sql(`SELECT name FROM providers WHERE id='${escSql(h.provider_id)}' AND app_type='${escSql(appType)}'`)[0];
    const name = provider ? provider.name : h.provider_id.slice(0, 8);
    const status = h.is_healthy ? green("healthy") : red("unhealthy");
    const failures = h.consecutive_failures > 0 ? red(`(${h.consecutive_failures} failures)`) : "";
    console.log(`  ${name.padEnd(25)} ${status} ${failures}`);
    if (h.last_error) console.log(dim(`    last error: ${h.last_error.slice(0, 80)}`));
  }
  console.log();
}

// ─── Arg parsing ───────────────────────────────────────────────────

function parseArgs(argv) {
  const args = argv.slice(2);
  let command = null;
  let positional = [];
  let flags = {};

  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--app" && i + 1 < args.length) {
      flags.app = args[++i];
    } else if (a === "--url" && i + 1 < args.length) {
      flags.url = args[++i];
    } else if (a === "--key" && i + 1 < args.length) {
      flags.key = args[++i];
    } else if (a.startsWith("-")) {
      flags[a] = true;
    } else if (!command) {
      command = a;
    } else {
      positional.push(a);
    }
  }
  return { command, positional, flags };
}

// ─── Main ──────────────────────────────────────────────────────────

function main() {
  const { command, positional, flags } = parseArgs(process.argv);
  const app = flags.app || "claude";

  if (!VALID_APPS.includes(app)) {
    console.error(`Invalid app: ${app}. Valid apps: ${VALID_APPS.join(", ")}`);
    process.exit(1);
  }

  switch (command) {
    case null:
      cmdDefault(flags.app ? [app] : DEFAULT_APPS);
      break;

    case "list":
    case "ls":
      cmdList(app);
      break;

    case "use":
    case "switch": {
      const name = positional[0];
      if (!name) {
        console.error("Usage: cc-switch use <provider-name> [--app claude]");
        process.exit(1);
      }
      cmdUse(app, name);
      break;
    }

    case "current":
    case "info":
      cmdCurrent(app);
      break;

    case "add": {
      const name = positional[0];
      if (!name || !flags.url) {
        console.error("Usage: cc-switch add <name> --url <url> --key <api-key> [--app claude]");
        process.exit(1);
      }
      cmdAdd(app, name, flags.url, flags.key || "");
      break;
    }

    case "rm":
    case "remove":
    case "delete": {
      const name = positional[0];
      if (!name) {
        console.error("Usage: cc-switch rm <provider-name> [--app claude]");
        process.exit(1);
      }
      cmdRm(app, name);
      break;
    }

    case "proxy": {
      const action = positional[0]; // "on" | "off" | undefined
      cmdProxy(app, action);
      break;
    }

    case "health":
      cmdHealth(app);
      break;

    case "help":
    case "--help":
    case "-h":
      console.log(`
${bold("cc-switch")} — CLI for CC Switch

${bold("Usage:")}
  npx cc-switch                                Show current providers
  npx cc-switch list [--app claude]             List all providers
  npx cc-switch use <name> [--app claude]       Switch to a provider
  npx cc-switch current [--app claude]          Show current provider details
  npx cc-switch add <name> --url <url> --key <key>  Add a provider
  npx cc-switch rm <name> [--app claude]        Remove a provider
  npx cc-switch proxy [on|off] [--app claude]   Show/toggle proxy
  npx cc-switch health [--app claude]           Show provider health

${bold("Apps:")} ${VALID_APPS.join(", ")}
${bold("Default:")} claude, codex

${bold("Examples:")}
  npx cc-switch use "CatPaw Relay"
  npx cc-switch use "美团Claude" --app claude
  npx cc-switch list --app codex
  npx cc-switch proxy on
`);
      break;

    default:
      console.error(`Unknown command: ${command}`);
      console.error(`Run "${bold("npx cc-switch help")}" for usage.`);
      process.exit(1);
  }
}

// Polyfill crypto.randomUUID for older Node
import { webcrypto as crypto } from "node:crypto";

main();
