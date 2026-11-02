// Read-only bridge: calls only the actual F009 parser, never storage/activation.
import { readFile, readdir, realpath } from 'node:fs/promises';
import { resolve, join, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
const [root, bundle, capJSON] = process.argv.slice(2);
const files = ['src/canonical-input.mjs','src/canonical-validation.mjs',
  'canonical-search-input-v1.schema.json','canonical-search-v1.schema.json','package-lock.json'];
const hashes = async () => Object.fromEntries(await Promise.all(files.map(async f =>
  [f, createHash('sha256').update(await readFile(join(root,f))).digest('hex')])));
const hash = async path => createHash('sha256').update(await readFile(path)).digest('hex');
async function installedHashes(directory, prefix = '') {
  const result = {};
  for (const entry of await readdir(directory, {withFileTypes:true})) {
    if (entry.name === '.bin') continue;
    const path = join(directory,entry.name), key = prefix + entry.name;
    if (entry.isDirectory()) Object.assign(result,await installedHashes(path,key+'/'));
    else if (entry.isFile() && /\.(?:[cm]?js|json)$/.test(entry.name)) result[key] = await hash(path);
    else if (entry.isSymbolicLink()) throw Error('unsupported_dependency_link');
  }
  return result;
}
try {
  if (!root || !bundle) throw Error('arguments');
  const before = await hashes();
  // The AJV CommonJS graph is loaded transitively by canonical-validation.
  // Capture installed bytes before import, then name/hash every executed module.
  const moduleRoot = await realpath(join(root,'node_modules'));
  const installed = await installedHashes(moduleRoot);
  const { readManifest, consumeBundle } = await import(pathToFileURL(resolve(root,'src/canonical-input.mjs')));
  const options = capJSON ? JSON.parse(capJSON) : undefined;
  const receipt = await readManifest(bundle,options);
  const counts = {main:0,building:0,address:0};
  await consumeBundle(bundle,receipt,async (component,start,batch) => { counts[component] += batch.length; },options);
  const after = await hashes();
  if (JSON.stringify(before)!==JSON.stringify(after)) throw Error('validator_changed');
  const executed = {};
  for (const path of Object.keys(createRequire(import.meta.url).cache).sort()) {
    if (!path.startsWith(moduleRoot+sep)) throw Error('external_dependency');
    const key = relative(moduleRoot,path).split(sep).join('/');
    const current = await hash(path);
    if (installed[key] !== current) throw Error('validator_changed');
    executed['node_modules/'+key] = current;
  }
  process.stdout.write(JSON.stringify({compatible:true,counts,validator_sha256:{...before,...executed}})+'\n');
} catch {
  process.stderr.write('{"compatible":false,"code":"infra_input_rejected"}\n');
  process.exitCode=1;
}
