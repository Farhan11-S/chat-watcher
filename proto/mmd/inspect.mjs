// Throwaway: print a PMX's bones and morphs.  node inspect.mjs model/X/X.pmx
import { readFileSync } from 'node:fs';
import { MMDParser } from './node_modules/three/examples/jsm/libs/mmdparser.module.js';
const buf = readFileSync(process.argv[2]);
const m = new MMDParser.Parser().parsePmx(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), true);
const types = ['group', 'vertex', 'bone', 'uv', 'uv1', 'uv2', 'uv3', 'uv4', 'material', 'flip', 'impulse'];
const panels = { 1: 'brow', 2: 'eye', 3: 'mouth', 4: 'other' };
console.log(`${m.metadata.modelName} | ${m.vertices.length} vertices, ${m.bones.length} bones, ${m.morphs.length} morphs, ${m.materials.length} materials, ${m.rigidBodies.length} rigid bodies`);
const by = {};
for (const x of m.morphs) (by[`${panels[x.panel] || x.panel}/${types[x.type]}`] ||= []).push(x.name);
for (const [k, v] of Object.entries(by)) console.log(`\n[${k}] ${v.join(' ')}`);
const want = ['センター', '上半身', '上半身2', '首', '頭', '左肩', '左腕', '左ひじ', '左手首', '右肩', '右腕', '右ひじ', '右手首', '両目', '左目', '右目'];
console.log('\nbones present:', want.filter((n) => m.bones.some((b) => b.name === n)).join(' '));
console.log('bones absent:', want.filter((n) => !m.bones.some((b) => b.name === n)).join(' ') || 'none');
const pos = (n) => m.bones.find((b) => b.name === n)?.position.map((v) => +v.toFixed(2));
console.log('頭', pos('頭'), '左腕', pos('左腕'), '左ひじ', pos('左ひじ'), '左手首', pos('左手首'));
console.log('textures:', m.textures.join(' | '));
