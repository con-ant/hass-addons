#!/usr/bin/env node
// Splice wasm-shim.html into the ttyd client's inline.html, immediately
// before its single bundled <script>. Runs in the ttyd-client build stage
// (see Dockerfile); the result is what ttyd serves via --index.
//
// Fails loudly instead of producing a silently unshimmed page: the marker
// must occur exactly once, so a future ttyd client that changes its script
// tag (or grows a second one) breaks the build rather than the add-on.
//
// Usage: inject-wasm-shim.js <inline.html> <wasm-shim.html>
'use strict';
const fs = require('fs');

const [, , htmlPath, shimPath] = process.argv;
if (!htmlPath || !shimPath) {
  console.error('usage: inject-wasm-shim.js <inline.html> <wasm-shim.html>');
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, 'utf8');
const shim = fs.readFileSync(shimPath, 'utf8');
const marker = '<script type="text/javascript">';

const first = html.indexOf(marker);
if (first < 0) {
  console.error(`inject-wasm-shim: marker ${JSON.stringify(marker)} not found in ${htmlPath}`);
  process.exit(1);
}
if (html.indexOf(marker, first + 1) >= 0) {
  console.error(`inject-wasm-shim: marker ${JSON.stringify(marker)} occurs more than once in ${htmlPath}`);
  process.exit(1);
}
if (html.includes('__ttydShim')) {
  console.error(`inject-wasm-shim: ${htmlPath} already contains the shim`);
  process.exit(1);
}

fs.writeFileSync(htmlPath, html.slice(0, first) + shim + html.slice(first));
console.log(`inject-wasm-shim: inserted ${shim.length} bytes into ${htmlPath}`);
