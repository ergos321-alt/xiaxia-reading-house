import { createServer } from 'node:http'
import { readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'
import { createRequire } from 'node:module'

const { chromium } = createRequire(join(process.cwd(), 'phase3-stateful-browser.cjs'))('playwright')
const root = process.cwd()
const fixture = process.env.PHASE3_BROWSER_EPUB
if (!fixture || !statSync(fixture).isFile()) throw new Error('PHASE3_BROWSER_EPUB is required')

const sourceHash = 'a'.repeat(64)
const textA = '此时相望不相闻，愿逐月华流照君'
const textB = '后来选择的那一句话'
const textShort = '君子好逑'
const href = 'Text/chapter.xhtml'
const state = { annotations: [], thoughts: [], nextAnnotation: 1 }
let baseURL = ''

const html = `<!doctype html><html><body
 data-book-id="book-1" data-text-index-ready="true"
 data-reader-engine-enabled="true" data-dual-anchor-enabled="true">
 <div id="reader-progress"><span></span></div><span id="chapter-position"></span>
 <strong id="reader-book-title"></strong><span id="reader-chapter-title"></span>
 <main><section id="chapter-thoughts" hidden></section><article id="chapter-content"></article>
 <section id="finish-book-entry" hidden><button id="finish-book-button"></button></section></main>
 <div id="page-status" hidden></div><div id="foliate-readonly-note" hidden></div>
 <button id="page-previous"></button><button id="page-next"></button>
 <button id="previous-chapter"></button><button id="next-chapter"></button>
 <button id="reading-mode"></button><button id="font-down"></button><button id="font-up"></button>
 <button id="toc-button"></button><button id="toc-close"></button><div id="toc-drawer"></div>
 <div id="toc-list"></div><div id="drawer-scrim" hidden></div>
 <button id="reader-menu-button"></button><div id="reader-menu" hidden></div>
 <div id="selection-menu" hidden><button id="highlight-selection">只划线</button>
 <button id="note-selection">留一句</button></div>
 <dialog id="note-dialog"><form id="note-form"><h2 id="note-dialog-title"></h2>
 <blockquote id="selected-quote"></blockquote><textarea id="note-text"></textarea>
 <button type="button" data-close-note>取消</button><button id="save-note" type="submit">保存</button></form></dialog>
 <dialog id="annotation-dialog"><form id="annotation-view-form"><p id="annotation-kind"></p>
 <blockquote id="annotation-quote"></blockquote><div id="trace-record-choices" hidden></div>
 <section id="user-note-section"><p id="annotation-user-note"></p></section>
 <section id="xiaxia-reply-section"><p id="annotation-xiaxia-reply"></p></section>
 <section id="xiaxia-thought-section"><p id="annotation-xiaxia-thought"></p></section>
 <section id="thought-user-reply-section"><p id="thought-user-reply"></p></section>
 <div id="thought-actions"></div><div id="annotation-actions"></div>
 <div id="trace-actions-menu" hidden></div><button id="trace-menu-button" type="button"></button>
 <button id="edit-annotation" type="button"></button><button id="delete-annotation" type="button"></button>
 <button id="thought-reply-edit" type="button"></button><button id="thought-reply-delete" type="button"></button>
 </form></dialog>
 <script type="module">import('/static/js/foliate-reader.js')</script>
 </body></html>`

const json = (response, value, status = 200) => {
    response.writeHead(status, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(value))
}

const body = request => new Promise(resolve => {
    const chunks = []
    request.on('data', chunk => chunks.push(chunk))
    request.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString() || '{}')) }
        catch { resolve({}) }
    })
})

const server = createServer(async (request, response) => {
    const url = new URL(request.url, baseURL || 'http://127.0.0.1')
    const pathname = url.pathname
    if (pathname === '/') {
        response.writeHead(200, { 'Content-Type': 'text/html' })
        response.end(html)
        return
    }
    if (pathname === '/fixture.epub') {
        response.writeHead(200, { 'Content-Type': 'application/epub+zip' })
        response.end(readFileSync(fixture))
        return
    }
    if (pathname === '/api/books/book-1' && request.method === 'GET') {
        json(response, { book: {
            id: 'book-1', title: 'Stateful Test', locator_bridge_status: 'ready',
            locator_bridge_version: 1, publication_progress: null,
        } })
        return
    }
    if (pathname === '/api/books/book-1/source-access') {
        json(response, { signed_url: `${baseURL}/fixture.epub`, book: {
            filename: 'fixture.epub', source_sha256: sourceHash,
        } })
        return
    }
    if (pathname === '/api/books/book-1/engine-traces' && request.method === 'GET') {
        json(response, { annotations: state.annotations, xiaxia_thoughts: state.thoughts })
        return
    }
    if (pathname === '/api/books/book-1/engine-traces/revalidate') {
        json(response, { status: 'revalidated', mode: 'integrity_check', cleared: 0 })
        return
    }
    const locatorMatch = pathname.match(/^\/api\/books\/book-1\/engine-traces\/(annotation|thought)\/([^/]+)\/locator$/)
    if (locatorMatch && request.method === 'PUT') {
        const payload = await body(request)
        const collection = locatorMatch[1] === 'thought' ? state.thoughts : state.annotations
        const record = collection.find(item => item.id === locatorMatch[2])
        record.engine_locator = payload.engine_locator
        record.engine_locator_version = 1
        json(response, { record })
        return
    }
    if (pathname === '/api/annotations' && request.method === 'POST') {
        const payload = await body(request)
        const id = `u${state.nextAnnotation++}`
        const record = {
            id, selected_text: payload.engine_locator.text.highlight,
            comment: payload.comment || '', start_block_id: 'b000001',
            start_offset: 0, end_block_id: 'b000001',
            end_offset: payload.engine_locator.text.highlight.length,
            engine_locator: payload.engine_locator, engine_locator_version: 1,
        }
        state.annotations.push(record)
        json(response, { annotation: record }, 201)
        return
    }
    if (pathname === '/api/books/book-1/publication-progress' || pathname === '/api/books/book-1/completion') {
        json(response, { status: 'ok' })
        return
    }
    if (pathname === '/test/thought' && request.method === 'POST') {
        state.thoughts.push(await body(request))
        json(response, { status: 'created' }, 201)
        return
    }
    if (pathname === '/test/state') {
        json(response, state)
        return
    }
    const path = join(root, normalize(pathname).replace(/^[/\\]+/, ''))
    if (!path.startsWith(root)) {
        response.writeHead(403).end()
        return
    }
    try {
        const type = extname(path) === '.js' ? 'text/javascript' : 'application/octet-stream'
        response.writeHead(200, { 'Content-Type': type })
        response.end(readFileSync(path))
    } catch {
        response.writeHead(404).end()
    }
})

await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
baseURL = `http://127.0.0.1:${server.address().port}`

const browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath() })
try {
    const page = await browser.newPage({
        viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true,
        userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/127 Mobile Safari/537.36',
    })
    const pageErrors = []
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.goto(`${baseURL}/?selection_debug=1`)
    await page.waitForFunction(() => globalThis.__readingHouseSelectionDebug?.adapter?.view)

    const selectText = async target => page.evaluate(async expected => {
        const debug = globalThis.__readingHouseSelectionDebug
        const doc = debug.adapter.view.renderer.getContents()[0].doc
        const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT)
        let node
        while ((node = walker.nextNode())) {
            const start = node.data.indexOf(expected)
            if (start < 0) continue
            const range = doc.createRange()
            range.setStart(node, start)
            range.setEnd(node, start + expected.length)
            const selection = doc.defaultView.getSelection()
            selection.removeAllRanges()
            selection.addRange(range)
            doc.dispatchEvent(new Event('selectionchange'))
            doc.dispatchEvent(new PointerEvent('pointerup'))
            await new Promise(resolve => setTimeout(resolve, 420))
            return debug.getState()
        }
        throw new Error(`text not found: ${expected}`)
    }, target)

    // Sequence E/S1: selection handle touch events must not move the paginator.
    let selected = await selectText(textShort)
    const pageBefore = selected.adapter.page_before?.cfi
    const touchResult = await page.evaluate(async expected => {
        const debug = globalThis.__readingHouseSelectionDebug
        const doc = debug.adapter.view.renderer.getContents()[0].doc
        const dispatch = (type, x) => {
            const event = new Event(type, { bubbles: true, cancelable: true })
            const touch = { screenX: x, screenY: 400 }
            Object.defineProperties(event, {
                changedTouches: { value: [touch] },
                touches: { value: type === 'touchend' ? [] : [touch] },
            })
            doc.dispatchEvent(event)
        }
        dispatch('touchstart', 200)
        dispatch('touchmove', 120)
        dispatch('touchend', 120)
        await new Promise(resolve => setTimeout(resolve, 420))
        const state = debug.getState()
        return {
            selected: state.saved_selection?.text?.highlight,
            pageBefore: state.adapter.page_before?.cfi,
            pageAfter: state.adapter.page_after?.cfi,
            expected,
        }
    }, textShort)

    // Sequence A/D: modal focus replaces live DOM selection, frozen snapshot survives.
    await page.click('#note-selection')
    await page.fill('#note-text', '关于君子好逑的想法')
    const frozen = await page.evaluate(() => globalThis.__readingHouseSelectionDebug.getState())
    await page.click('#save-note')
    await page.waitForFunction(() => globalThis.__readingHouseSelectionDebug.getState().save_stage === 'decoration_drawn')
    let persisted = await page.evaluate(() => fetch('/test/state').then(response => response.json()))
    const firstAnnotation = persisted.annotations[0]

    // Refresh restores the record-specific decoration.
    await page.reload()
    await page.waitForFunction(() => globalThis.__readingHouseSelectionDebug?.adapter?.decorations?.has('annotation:u1'))

    // Sequence B: legacy Thought backfills its own locator, then a later user record is added.
    await page.evaluate(async ({ textA, textB, href, sourceHash }) => {
        const seed = {
            engine: 'foliate-js', engine_adapter_version: 1, bridge_version: 1,
            source_sha256: sourceHash, href, spine_index: 0,
            original_start: textA.length + 1,
            original_end: textA.length + 1 + textB.length,
            text: { highlight: textB, before: textA, after: '' },
        }
        await fetch('/test/thought', { method: 'POST', body: JSON.stringify({
            id: 't1', scope: 'range', selected_text: textB, content: '林知夏的想法',
            start_block_id: 'b000002', start_offset: 0,
            end_block_id: 'b000002', end_offset: textB.length,
            locator_seed: seed, engine_locator: null,
        }) })
        await globalThis.__readingHouseSelectionDebug.refreshTraces()
    }, { textA, textB, href, sourceHash })
    await page.waitForFunction(() => globalThis.__readingHouseSelectionDebug.adapter.decorations.has('thought:t1'))

    // Sequence C: cancelled first Range cannot contaminate the second selection.
    await selectText(textShort)
    await page.evaluate(() => globalThis.__readingHouseSelectionDebug.adapter.clearBrowserSelection())
    await selectText(textA)
    await page.click('#note-selection')
    await page.fill('#note-text', '第二条')
    await page.click('#save-note')
    await page.waitForFunction(() => globalThis.__readingHouseSelectionDebug.adapter.decorations.has('annotation:u2'))

    persisted = await page.evaluate(() => fetch('/test/state').then(response => response.json()))
    const thought = persisted.thoughts[0]
    const secondAnnotation = persisted.annotations[1]
    const navigation = await page.evaluate(async ({ textA, textB }) => {
        const debug = globalThis.__readingHouseSelectionDebug
        await debug.navigateToTrace('xiaxia', 't1', { open: false })
        const thought = await fetch('/test/state').then(response => response.json()).then(x => x.thoughts[0])
        const verifiedThought = await debug.adapter.verifyLocator(thought.engine_locator, textB)
        await debug.navigateToTrace('user', 'u2', { open: false })
        const annotation = await fetch('/test/state').then(response => response.json()).then(x => x.annotations[1])
        const verifiedAnnotation = await debug.adapter.verifyLocator(annotation.engine_locator, textA)
        await debug.navigateToTrace('xiaxia', 't1', { open: false })
        const verifiedAgain = await debug.adapter.verifyLocator(thought.engine_locator, textB)
        return {
            thought: verifiedThought.text,
            annotation: verifiedAnnotation.text,
            thoughtAgain: verifiedAgain.text,
        }
    }, { textA, textB })

    // Sequence F: same CFI retains two identities and opens an explicit chooser.
    await page.evaluate(async annotation => {
        await fetch('/test/thought', { method: 'POST', body: JSON.stringify({
            id: 't2', scope: 'range', selected_text: annotation.selected_text,
            content: '同一处的另一条记录', start_block_id: 'b000001', start_offset: 0,
            end_block_id: 'b000001', end_offset: annotation.selected_text.length,
            engine_locator: annotation.engine_locator, locator_seed: null,
        }) })
        await globalThis.__readingHouseSelectionDebug.refreshTraces()
        await globalThis.__readingHouseSelectionDebug.adapter.view.showAnnotation({
            value: annotation.engine_locator.cfi,
        })
    }, firstAnnotation)
    const sameCfiKeys = await page.locator('#trace-record-choices button').evaluateAll(
        buttons => buttons.map(button => button.dataset.recordKey).sort(),
    )

    process.stdout.write(JSON.stringify({
        touchResult, pageBefore,
        frozenText: frozen.pending_selection?.text?.highlight,
        liveSelectionAfterModal: frozen.saved_selection,
        firstSavedText: firstAnnotation.selected_text,
        secondSavedText: secondAnnotation.selected_text,
        thoughtLocatorText: thought.engine_locator?.browser_truth?.text,
        navigation,
        sameCfiKeys,
        decorationKeys: await page.evaluate(() => [
            ...globalThis.__readingHouseSelectionDebug.adapter.decorations.keys(),
        ].sort()),
        pageErrors,
    }))
} finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
}
