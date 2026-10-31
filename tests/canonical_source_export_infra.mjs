// Read-only bridge: calls only the actual F009 parser, never storage/activation.
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
const [root, bundle, capJSON] = process.argv.slice(2);
const files = ['src/canonical-input.mjs','src/canonical-validation.mjs',
  'canonical-search-input-v1.schema.json','canonical-search-v1.schema.json','package-lock.json'];
const hashes = async () => Object.fromEntries(await Promise.all(files.map(async f =>
  [f, createHash('sha256').update(await readFile(join(root,f))).digest('hex')])));
try {
  if (!root || !bundle) throw Error('arguments');
  const before = await hashes();
  const { readManifest, consumeBundle } = await import(pathToFileURL(resolve(root,'src/canonical-input.mjs')));
  const options = capJSON ? JSON.parse(capJSON) : undefined;
  const receipt = await readManifest(bundle,options);
  const counts = {main:0,building:0,address:0};
  await consumeBundle(bundle,receipt,async (component,start,batch) => { counts[component] += batch.length; },options);
  const after = await hashes();
  if (JSON.stringify(before)!==JSON.stringify(after)) throw Error('validator_changed');
  process.stdout.write(JSON.stringify({compatible:true,counts,validator_sha256:before})+'\n');
} catch {
  process.stderr.write('{"compatible":false,"code":"infra_input_rejected"}\n');
  process.exitCode=1;
}
