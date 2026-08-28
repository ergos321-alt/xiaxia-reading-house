import {
    attachSafeLinkPolicy,
    closePublication,
    createSecurePublication,
    downloadAsFile,
    drawMemoryHighlight,
    probeRangeSupport,
    readApproximateMemory,
} from './foliate-poc-adapter.js'

const $ = selector => document.querySelector(selector)
const bookId = document.body.dataset.bookId
const host = $('#poc-reader-host')
const eventLog = $('#poc-event-log')
const status = $('#poc-status')
const cfiOutput = $('#poc-cfi')
const selectionOutput = $('#poc-selection')
const tocList = $('#poc-toc-list')

let view = null
let sourceKey = ''
let fontSize = 18
let flow = 'paginated'
let selectedCFI = ''
let abortController = null
let firstRelocateResolve = null
let pendingInteraction = null
let openedAt = 0

const emptyResult = () => ({
    file: '',
    size: null,
    epub_version: null,
    spine_items: null,
    images: null,
    fonts: null,
    nav_type: null,
    source_mode: null,
    range_probe: null,
    open_result: 'PENDING',
    first_render_ms: null,
    toc: 'PENDING',
    next_prev: 'PENDING',
    pagination: 'PENDING',
    scroll: 'PENDING',
    image_rendering: 'PENDING',
    internal_links: 'PENDING',
    footnotes: 'PENDING',
    CFI_create: 'PENDING',
    CFI_restore: 'PENDING',
    resize: 'PENDING',
    font_change: 'PENDING',
    orientation: 'PENDING',
    browser_memory: [],
    error: '',
    workaround: 'NONE',
})
let result = emptyResult()

const log = (name, detail = {}) => {
    const entry = {
        at: new Date().toISOString(),
        name,
        detail,
    }
    const line = document.createElement('div')
    line.textContent = JSON.stringify(entry)
    eventLog.prepend(line)
    while (eventLog.children.length > 120) eventLog.lastElementChild.remove()
}

const setStatus = (message, state = '') => {
    status.textContent = message
    status.dataset.state = state
}

const localizedText = value => {
    if (!value) return ''
    if (typeof value === 'string') return value
    return value[Object.keys(value)[0]] ?? ''
}

const memorySample = stage => {
    const sample = {
        stage,
        at: new Date().toISOString(),
        ...readApproximateMemory(),
    }
    result.browser_memory.push(sample)
    log('memory', sample)
    return sample
}

const storageKey = suffix => `xiaxia-foliate-poc:${sourceKey}:${suffix}`

const stylePublication = () => {
    view?.renderer?.setAttribute('flow', flow)
    view?.renderer?.setStyles?.(`
        html { color-scheme: light; }
        body {
            font-size: ${fontSize}px !important;
            line-height: 1.86 !important;
            color: #39342f;
            background: #f8f2e7;
        }
        img, svg, table { max-width: 100% !important; }
        pre { white-space: pre-wrap !important; }
    `)
}

const flattenTOC = (items, depth = 0, output = []) => {
    for (const item of items ?? []) {
        output.push({ ...item, depth })
        flattenTOC(item.subitems ?? item.children ?? [], depth + 1, output)
    }
    return output
}

const renderTOC = book => {
    tocList.replaceChildren()
    const items = flattenTOC(book.toc)
    result.toc = items.length ? 'PASS' : 'FAIL'
    for (const item of items) {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'poc-toc-item'
        button.style.setProperty('--toc-depth', item.depth)
        button.textContent = localizedText(item.label) || item.href || 'Untitled'
        button.addEventListener('click', async () => {
            await view.goTo(item.href)
            log('toc-go-to', { href: item.href })
        })
        tocList.append(button)
    }
}

const updateBookFacts = (book, file) => {
    const manifest = book.resources?.manifest ?? []
    result.file = file.name
    result.size = file.size
    result.epub_version = book.resources?.opf?.documentElement
        ?.getAttribute('version') ?? null
    result.spine_items = book.sections?.length ?? 0
    result.images = manifest.filter(item => item.mediaType?.startsWith('image/')).length
    result.fonts = manifest.filter(item => item.mediaType?.startsWith('font/')
        || /(?:woff2?|opentype|truetype)/.test(item.mediaType ?? '')).length
    result.nav_type = book.resources?.navPath
        ? 'NAV'
        : book.resources?.ncxPath ? 'NCX' : 'fallback'
    result.image_rendering = result.images ? 'PENDING' : 'N/A'
    $('#poc-book-title').textContent = localizedText(book.metadata?.title)
        || file.name
    $('#poc-source-facts').textContent = [
        `${(file.size / 1024 / 1024).toFixed(2)} MB`,
        `${result.spine_items} spine`,
        `${result.images} images`,
        `${result.fonts} fonts`,
        result.nav_type,
    ].join(' · ')
}

const attachSelection = ({ doc, index }) => {
    const capture = () => {
        const selection = doc.defaultView.getSelection()
        if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return
        const range = selection.getRangeAt(0)
        const quote = selection.toString().trim()
        if (!quote) return
        selectedCFI = view.getCFI(index, range)
        const href = view.book.sections[index]?.id ?? ''
        const debug = { cfi: selectedCFI, href, sectionIndex: index, quote }
        selectionOutput.textContent = JSON.stringify(debug, null, 2)
        localStorage.setItem(storageKey('selection'), JSON.stringify(debug))
        result.CFI_create = 'PASS'
        log('selection', debug)
    }
    doc.addEventListener('pointerup', capture)
    doc.addEventListener('keyup', capture)

    const images = [...doc.images]
    Promise.all(images.map(image => image.complete
        ? Promise.resolve(image.naturalWidth > 0)
        : new Promise(resolve => {
            image.addEventListener('load', () => resolve(true), { once: true })
            image.addEventListener('error', () => resolve(false), { once: true })
        }))).then(values => {
        if (values.length) result.image_rendering = values.every(Boolean) ? 'PASS' : 'FAIL'
    })
}

const attachViewEvents = () => {
    view.addEventListener('load', event => {
        attachSelection(event.detail)
        log('section-load', {
            index: event.detail.index,
            href: view.book.sections[event.detail.index]?.id,
        })
    })
    view.addEventListener('relocate', event => {
        const detail = event.detail
        cfiOutput.textContent = detail.cfi ?? ''
        if (detail.cfi) localStorage.setItem(storageKey('last-cfi'), detail.cfi)
        if (result.first_render_ms == null) {
            result.first_render_ms = Math.round(performance.now() - openedAt)
            firstRelocateResolve?.()
            firstRelocateResolve = null
            memorySample('first-render')
        }
        if (pendingInteraction) {
            result[pendingInteraction] = 'PASS'
            pendingInteraction = null
        }
        log('relocate', {
            cfi: detail.cfi,
            fraction: detail.fraction,
            section: detail.section,
        })
    })
    view.addEventListener('draw-annotation', drawMemoryHighlight)
    view.addEventListener('create-overlay', () => {
        const saved = JSON.parse(localStorage.getItem(storageKey('highlights')) || '[]')
        saved.forEach(annotation => view.addAnnotation(annotation))
    })
    attachSafeLinkPolicy(view, (name, detail) => {
        if (name === 'internal-link') pendingInteraction = 'internal_links'
        if (name === 'footnote-link') pendingInteraction = 'footnotes'
        log(name, detail)
    })
}

const disposeCurrent = () => {
    abortController?.abort()
    abortController = null
    closePublication(view)
    view = null
    host.replaceChildren()
    memorySample('publication-closed')
}

const openFile = async (file, sourceMode, sourceDetail = {}) => {
    if (!file?.name?.toLowerCase().endsWith('.epub')) {
        throw new Error('请选择 EPUB 文件。')
    }
    disposeCurrent()
    result = emptyResult()
    openedAt = performance.now()
    sourceKey = sourceDetail.sourceKey
        || `${file.name}:${file.size}:${file.lastModified || 0}`
    result.source_mode = sourceMode
    result.range_probe = sourceDetail.rangeProbe ?? null
    result.error = ''
    result.open_result = 'PENDING'
    setStatus('foliate-js 正在打开原始 EPUB…')
    memorySample('before-open')

    const publication = await createSecurePublication(file)
    view = publication.view
    view.id = 'poc-foliate-view'
    host.replaceChildren(view)
    updateBookFacts(publication.book, file)
    renderTOC(publication.book)
    attachViewEvents()
    stylePublication()

    const firstRelocate = new Promise(resolve => {
        firstRelocateResolve = resolve
    })
    const lastCFI = localStorage.getItem(storageKey('last-cfi'))
    await view.init({ lastLocation: lastCFI || null, showTextStart: true })
    await Promise.race([
        firstRelocate,
        new Promise((_, reject) => setTimeout(
            () => reject(new Error('first_render_timeout')), 15000)),
    ])
    result.open_result = 'PASS'
    setStatus(`已打开 · ${sourceMode}`, 'success')
    log('publication-open', {
        sourceMode,
        rangeProbe: sourceDetail.rangeProbe,
        downloadMs: sourceDetail.downloadMs,
    })
}

const openStoredBook = async () => {
    if (!bookId) return
    abortController = new AbortController()
    setStatus('正在请求短时 private source access…')
    const response = await fetch(`/api/reader-engine-poc/books/${bookId}/source`, {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: abortController.signal,
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.error || `source_access_${response.status}`)

    let rangeProbe
    try {
        rangeProbe = await probeRangeSupport(payload.signed_url, abortController.signal)
    } catch (error) {
        rangeProbe = { supported: false, error: String(error) }
    }
    log('signed-url-range-probe', rangeProbe)

    // Pinned foliate-js fetches URL inputs as one Blob; its vendor zip bundle
    // has no remote HttpReader. Keep the transport result visible, then use
    // the mandated full-Blob fallback instead of labelling it as Range.
    const downloaded = await downloadAsFile(
        payload.signed_url,
        payload.book.filename,
        abortController.signal,
    )
    await openFile(downloaded.file, 'Blob', {
        rangeProbe,
        downloadMs: downloaded.downloadMs,
        sourceKey: `storage:${bookId}`,
    })
}

$('#poc-file-input').addEventListener('change', async event => {
    try {
        await openFile(event.target.files[0], 'Local File')
    } catch (error) {
        result.open_result = 'FAIL'
        result.error = String(error)
        setStatus(`打开失败：${error.message || error}`, 'error')
        log('open-error', { error: String(error), stack: error.stack })
    }
})

$('#poc-prev').addEventListener('click', async () => {
    if (!view) return
    pendingInteraction = flow === 'scrolled' ? 'scroll' : 'pagination'
    await view.prev()
    result.next_prev = result.next_prev === 'NEXT' ? 'PASS' : 'PREV'
})

$('#poc-next').addEventListener('click', async () => {
    if (!view) return
    pendingInteraction = flow === 'scrolled' ? 'scroll' : 'pagination'
    await view.next()
    result.next_prev = result.next_prev === 'PREV' ? 'PASS' : 'NEXT'
})

$('#poc-history-back').addEventListener('click', () => view?.history.back())

$('#poc-flow').addEventListener('change', event => {
    flow = event.target.value
    pendingInteraction = flow === 'scrolled' ? 'scroll' : 'pagination'
    stylePublication()
    log('flow-change', { flow })
})

$('#poc-font-down').addEventListener('click', () => {
    fontSize = Math.max(12, fontSize - 2)
    pendingInteraction = 'font_change'
    stylePublication()
})

$('#poc-font-up').addEventListener('click', () => {
    fontSize = Math.min(36, fontSize + 2)
    pendingInteraction = 'font_change'
    stylePublication()
})

$('#poc-restore-cfi').addEventListener('click', async () => {
    if (!view) return
    const saved = selectedCFI
        || JSON.parse(localStorage.getItem(storageKey('selection')) || '{}').cfi
        || localStorage.getItem(storageKey('last-cfi'))
    if (!saved) return setStatus('尚无可恢复的 CFI。', 'error')
    await view.goTo(saved)
    result.CFI_restore = 'PASS'
    log('cfi-restored', { cfi: saved })
})

$('#poc-highlight').addEventListener('click', async () => {
    if (!view || !selectedCFI) return setStatus('请先在书页中选择文字。', 'error')
    const annotation = { value: selectedCFI, color: '#8b5c45' }
    const saved = JSON.parse(localStorage.getItem(storageKey('highlights')) || '[]')
    if (!saved.some(item => item.value === selectedCFI)) saved.push(annotation)
    localStorage.setItem(storageKey('highlights'), JSON.stringify(saved))
    await view.addAnnotation(annotation)
    log('memory-highlight-added', { cfi: selectedCFI })
})

$('#poc-memory').addEventListener('click', () => memorySample('manual'))
$('#poc-close').addEventListener('click', disposeCurrent)

$('#poc-export').addEventListener('click', () => {
    const payload = {
        ...result,
        user_agent: navigator.userAgent,
        viewport: {
            width: innerWidth,
            height: innerHeight,
            visualViewportHeight: visualViewport?.height ?? null,
            devicePixelRatio,
        },
        exported_at: new Date().toISOString(),
    }
    const url = URL.createObjectURL(new Blob(
        [JSON.stringify(payload, null, 2)],
        { type: 'application/json' },
    ))
    const link = document.createElement('a')
    link.href = url
    link.download = `foliate-poc-${result.file || 'result'}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
})

let resizeTimer
addEventListener('resize', () => {
    clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
        pendingInteraction = 'resize'
        log('viewport-resize', {
            width: innerWidth,
            height: innerHeight,
            visualViewportHeight: visualViewport?.height ?? null,
        })
    }, 160)
})

addEventListener('orientationchange', () => {
    pendingInteraction = 'orientation'
    log('orientation-change', { angle: screen.orientation?.angle ?? null })
})

window.__FOLIATE_POC__ = Object.freeze({
    result: () => structuredClone(result),
    selectFirstText: () => {
        if (!view) throw new Error('publication_not_open')
        const content = view.renderer.getContents()[0]
        const walker = content.doc.createTreeWalker(
            content.doc.body,
            NodeFilter.SHOW_TEXT,
            node => node.data.trim().length >= 8
                ? NodeFilter.FILTER_ACCEPT
                : NodeFilter.FILTER_SKIP,
        )
        const node = walker.nextNode()
        if (!node) throw new Error('readable_text_not_found')
        const range = content.doc.createRange()
        const start = node.data.search(/\S/)
        range.setStart(node, start)
        range.setEnd(node, Math.min(node.length, start + 8))
        selectedCFI = view.getCFI(content.index, range)
        const debug = {
            cfi: selectedCFI,
            href: view.book.sections[content.index]?.id ?? '',
            sectionIndex: content.index,
            quote: range.toString(),
        }
        result.CFI_create = 'PASS'
        localStorage.setItem(storageKey('selection'), JSON.stringify(debug))
        selectionOutput.textContent = JSON.stringify(debug, null, 2)
        return debug
    },
    restoreSelected: async () => {
        if (!view || !selectedCFI) throw new Error('selection_cfi_not_available')
        await view.goTo(selectedCFI)
        result.CFI_restore = 'PASS'
        return view.lastLocation?.cfi ?? ''
    },
    addSelectedHighlight: async () => {
        if (!view || !selectedCFI) throw new Error('selection_cfi_not_available')
        const annotation = { value: selectedCFI, color: '#8b5c45' }
        await view.addAnnotation(annotation)
        return annotation
    },
    goToSection: async index => {
        if (!view) throw new Error('publication_not_open')
        await view.goTo(Number(index))
        memorySample(`section-${Number(index)}`)
        return view.lastLocation?.cfi ?? ''
    },
    memorySample,
    close: disposeCurrent,
})

memorySample('page-loaded')
if (bookId) {
    openStoredBook().catch(error => {
        result.open_result = 'FAIL'
        result.error = String(error)
        setStatus(`Storage source 打开失败：${error.message || error}`, 'error')
        log('stored-source-error', { error: String(error), stack: error.stack })
    })
}
