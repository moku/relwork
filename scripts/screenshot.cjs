// Render graph.html (network view) to a PNG with headless Chrome.
// usage: node scripts/screenshot.cjs <puppeteer_dir> <html> <png>
const [pdir, htmlPath, pngPath] = process.argv.slice(2);
const puppeteer = require(`${pdir}/node_modules/puppeteer`);
(async () => {
  const b = await puppeteer.launch({args: ['--no-sandbox']});
  const p = await b.newPage();
  await p.setViewport({width: 1600, height: 1100, deviceScaleFactor: 2});
  await p.goto(`file://${htmlPath}`, {waitUntil: 'networkidle0'});
  await new Promise(r => setTimeout(r, 1500));
  await p.screenshot({path: pngPath});
  await b.close();
})();
