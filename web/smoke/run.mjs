// jsdom + esbuild 渲染冒烟入口。真实 fetch localhost:8000。
// 用法：先启动后端（DEV_USER=zhongyue3 … uvicorn），然后 `node web/smoke/run.mjs`
import { JSDOM } from 'jsdom'
import { buildSync } from 'esbuild'
import { createRequire } from 'module'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const dom = new JSDOM('<!DOCTYPE html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost:8000/portfolio',
})
global.window = dom.window
global.document = dom.window.document
global.localStorage = dom.window.localStorage
global.location = dom.window.location
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true })
global.HTMLElement = dom.window.HTMLElement
global.Node = dom.window.Node

process.env.NODE_ENV = 'production'

buildSync({
  entryPoints: [path.join(__dirname, 'entry.jsx')],
  outfile: path.join(__dirname, 'bundle.cjs'),
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node20',
  jsx: 'automatic',
  logLevel: 'warning',
  define: { 'process.env.NODE_ENV': '"production"' },
})

const require = createRequire(import.meta.url)
const { main } = require(path.join(__dirname, 'bundle.cjs'))
main().then((r) => {
  console.log('smoke summary:', r)
  process.exit(r.ok ? 0 : 1)
}).catch((e) => {
  console.error('SMOKE 崩溃:', e.message)
  console.error((e.stack || '').split('\n').slice(0, 8).join('\n'))
  process.exit(1)
})
