import { createServer } from 'node:http'
import { readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'
import { createRequire } from 'node:module'

const { chromium } = createRequire(join(process.cwd(), 'phase3-browser.cjs'))('playwright')

const root = process.cwd()
const fixture = process.env.PHASE3_BROWSER_EPUB
if (!fixture || !statSync(fixture).isFile()) throw new Error('PHASE3_BROWSER_EPUB is required')

const mime = path => ({
    '.js': 'text/javascript', '.html': 'text/html', '.epub': 'application/epub+zip',
}[extname(path)] || 'application/octet-stream')

const server = createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    if (pathname === '/') {
        response.writeHead(200, { 'Content-Type': 'text/html' })
        response.end('<!doctype html><main id="host" style="height:700px"></main>')
        return
    }
    const path = pathname === '/fixture.epub'
        ? fixture : join(root, normalize(pathname).replace(/^[/\\]+/, ''))
    if (path !== fixture && !path.startsWith(root)) {
        response.writeHead(403).end()
        return
    }
    try {
        response.writeHead(200, { 'Content-Type': mime(path) })
        response.end(readFileSync(path))
    } catch {
        response.writeHead(404).end()
    }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const baseURL = `http://127.0.0.1:${server.address().port}`

const browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath() })
try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })
    await page.goto(baseURL)
    const result = await page.evaluate(async base => {
        const { FoliateReaderAdapter } = await import(`${base}/static/js/foliate-reader-adapter.js`)
        const host = document.querySelector('#host')
        const adapter = new FoliateReaderAdapter(host)
        const bytes = await fetch(`${base}/fixture.epub`).then(response => response.arrayBuffer())
        await adapter.openPublication(new File([bytes], 'fixture.epub', { type: 'application/epub+zip' }))

        const textA = '此时相望不相闻，愿逐月华流照君'
        const textB = '后来选择的那一句话'
        const href = 'Text/chapter.xhtml'
        const baseSeed = {
            engine: 'foliate-js', engine_adapter_version: 1, bridge_version: 1,
            source_sha256: 'a'.repeat(64), href, spine_index: 0,
        }
        const locatorA = await adapter.locatorFromSeed({
            ...baseSeed, original_start: 0, original_end: textA.length,
            text: { highlight: textA, before: '', after: textB },
        }, { navigate: true })
        const locatorB = await adapter.locatorFromSeed({
            ...baseSeed, original_start: textA.length + 1,
            original_end: textA.length + 1 + textB.length,
            text: { highlight: textB, before: textA, after: '' },
        }, { navigate: true })

        const hits = []
        adapter.onAnnotation(({ records }) => hits.push(records.map(item => item.recordKey).sort()))
        await adapter.addDecoration({ id: 'A', kind: 'xiaxia', selected_text: textA, engine_locator: locatorA })
        await adapter.addDecoration({ id: 'B', kind: 'user', selected_text: textB, engine_locator: locatorB })
        await adapter.view.showAnnotation({ value: locatorA.cfi })
        await adapter.view.showAnnotation({ value: locatorB.cfi })

        // A later record at the exact same CFI remains a separate identity.
        await adapter.addDecoration({ id: 'C', kind: 'user', selected_text: textA, engine_locator: locatorA })
        await adapter.view.showAnnotation({ value: locatorA.cfi })

        const verifiedA = await adapter.verifyLocator(locatorA, textA, { navigate: true })
        const verifiedB = await adapter.verifyLocator(locatorB, textB, { navigate: true })
        let wrongTextError = null
        try { await adapter.verifyLocator(locatorA, textB, { navigate: true }) }
        catch (error) { wrongTextError = error.code || error.message }
        let wrongSectionError = null
        try {
            await adapter.locatorFromSeed({
                ...baseSeed, href: 'Text/missing.xhtml', spine_index: 99,
                original_start: 0, original_end: 1,
                text: { highlight: '错' },
            })
        } catch (error) { wrongSectionError = error.code || error.message }

        const output = {
            locatorAText: locatorA.browser_truth.text,
            locatorBText: locatorB.browser_truth.text,
            verifiedAText: verifiedA.text,
            verifiedBText: verifiedB.text,
            hits,
            keys: [...adapter.decorations.keys()].sort(),
            wrongTextError,
            wrongSectionError,
        }
        adapter.closePublication()
        return output
    }, baseURL)
    process.stdout.write(JSON.stringify(result))
} finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
}
