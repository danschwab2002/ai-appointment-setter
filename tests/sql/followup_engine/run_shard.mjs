// La suite SQL de PGlite, partida en tandas para la CI (claude-ci-split-v1).
//
// La lista no se escribe dos veces: sale de scripts.test de package.json, en su
// orden, mas los scripts que la CI corre aparte (--also). Cada script cae en
// exactamente una tanda. El reparto es LPT sobre el peso medido de cada script
// (shard_weights.json, en segundos): de mas pesado a mas liviano, cada uno va a
// la tanda con menos carga. Un script sin peso medido toma la mediana de los
// medidos, asi uno nuevo entra solo y no hace falta tocar este archivo.
//
// Antes de correr, cada tanda arma el reparto entero y verifica que sea una
// particion: todos los scripts, una sola vez cada uno, y ninguna tanda vacia.
// Dentro de la tanda, los scripts corren en el orden de package.json, y el
// primero que falla corta la tanda con su codigo, como hace npm test con &&.
//
// Uso:
//   node run_shard.mjs --shard 2 --of 3 [--also x.mjs ...]   corre la tanda 2
//   node run_shard.mjs --plan --of 3 [--also x.mjs ...]      imprime el reparto (JSON)
//
// El reparto depende solo de la lista y de los pesos: es el mismo en la CI y en
// local, y no cambia entre corridas.

import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const COMMAND = /^node ([A-Za-z0-9_.-]+\.mjs)((?: --[a-z][a-z-]*)*)$/;

function fail(message) {
  console.error(`run_shard: ${message}`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = { shard: null, of: null, also: [], plan: false };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === '--plan') {
      args.plan = true;
    } else if (flag === '--shard' || flag === '--of' || flag === '--also') {
      const value = argv[i + 1];
      if (value === undefined) fail(`${flag} necesita un valor`);
      i += 1;
      if (flag === '--also') {
        args.also.push(value);
      } else {
        if (!/^[1-9][0-9]*$/.test(value)) fail(`${flag} tiene que ser un entero positivo: ${value}`);
        args[flag.slice(2)] = Number(value);
      }
    } else {
      fail(`argumento desconocido: ${flag}`);
    }
  }
  if (args.of === null) fail('falta --of');
  if (!args.plan && args.shard === null) fail('falta --shard (o --plan)');
  if (args.shard !== null && args.shard > args.of) fail(`--shard ${args.shard} es mayor que --of ${args.of}`);
  return args;
}

function parseCommand(text, index) {
  const match = COMMAND.exec(text);
  if (!match) fail(`no es "node <script>.mjs [--flag]": ${JSON.stringify(text)}`);
  const file = match[1];
  if (!existsSync(join(HERE, file))) fail(`no existe ${file}`);
  const flags = match[2].trim() === '' ? [] : match[2].trim().split(' ');
  return { index, text, file, flags };
}

function loadCommands(also) {
  const pkg = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'));
  const test = pkg?.scripts?.test;
  if (typeof test !== 'string' || test.trim() === '') fail('package.json no tiene scripts.test');
  // npm test correria un pretest o un posttest, y las tandas no: que no pase en silencio.
  for (const hook of ['pretest', 'posttest']) {
    if (pkg.scripts[hook] !== undefined) fail(`package.json tiene scripts.${hook}, que las tandas no corren`);
  }
  const texts = [...test.split(' && '), ...also.map((file) => `node ${file}`)];
  const commands = texts.map((text, index) => parseCommand(text.trim(), index));
  const seen = new Set();
  for (const command of commands) {
    if (seen.has(command.text)) fail(`el script aparece dos veces: ${command.text}`);
    seen.add(command.text);
  }
  return commands;
}

function loadWeights(commands) {
  const raw = JSON.parse(readFileSync(join(HERE, 'shard_weights.json'), 'utf8'));
  const weights = raw.seconds;
  if (weights === null || typeof weights !== 'object') fail('shard_weights.json no tiene "seconds"');
  const known = Object.values(weights);
  for (const value of known) {
    if (typeof value !== 'number' || !(value > 0)) fail(`peso invalido en shard_weights.json: ${value}`);
  }
  const sorted = [...known].sort((a, b) => a - b);
  const median = sorted.length === 0 ? 1 : sorted[Math.floor(sorted.length / 2)];
  return commands.map((command) => weights[command.text] ?? median);
}

export function plan(commands, weights, of) {
  const order = commands
    .map((command, i) => ({ command, weight: weights[i] }))
    .sort((a, b) => b.weight - a.weight || a.command.index - b.command.index);
  const shards = Array.from({ length: of }, () => ({ load: 0, commands: [] }));
  for (const item of order) {
    let target = 0;
    for (let s = 1; s < of; s += 1) {
      if (shards[s].load < shards[target].load) target = s;
    }
    shards[target].load += item.weight;
    shards[target].commands.push(item.command);
  }
  for (const shard of shards) shard.commands.sort((a, b) => a.index - b.index);
  return shards;
}

function checkPartition(commands, shards) {
  const counts = new Map(commands.map((command) => [command.text, 0]));
  for (const [i, shard] of shards.entries()) {
    if (shard.commands.length === 0) fail(`la tanda ${i + 1} quedo vacia: sobran tandas para ${commands.length} scripts`);
    for (const command of shard.commands) counts.set(command.text, counts.get(command.text) + 1);
  }
  for (const [text, count] of counts) {
    if (count !== 1) fail(`${text} quedo en ${count} tandas`);
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const commands = loadCommands(args.also);
  const shards = plan(commands, loadWeights(commands), args.of);
  checkPartition(commands, shards);

  if (args.plan) {
    const out = shards.map((shard, i) => ({
      shard: i + 1,
      estimated_seconds: Math.round(shard.load),
      commands: shard.commands.map((command) => command.text),
    }));
    console.log(JSON.stringify({ total: commands.length, shards: out }, null, 2));
    return;
  }

  const mine = shards[args.shard - 1];
  console.log(`tanda ${args.shard} de ${args.of}: ${mine.commands.length} de ${commands.length} scripts, unos ${Math.round(mine.load)} s`);
  const started = Date.now();
  for (const command of mine.commands) {
    const t0 = Date.now();
    const result = spawnSync(process.execPath, [command.file, ...command.flags], { cwd: HERE, stdio: 'inherit' });
    const seconds = ((Date.now() - t0) / 1000).toFixed(1);
    if (result.error) {
      console.error(`run_shard: no se pudo correr ${command.text}: ${result.error.message}`);
      process.exit(1);
    }
    if (result.status !== 0) {
      const code = result.status ?? 1;
      console.error(`run_shard: fallo ${command.text} (codigo ${code}${result.signal ? `, senal ${result.signal}` : ''}) a los ${seconds} s`);
      process.exit(code);
    }
    console.log(`run_shard: ok ${seconds} s  ${command.text}`);
  }
  console.log(`tanda ${args.shard} de ${args.of}: ok en ${((Date.now() - started) / 1000).toFixed(0)} s`);
}

main();
