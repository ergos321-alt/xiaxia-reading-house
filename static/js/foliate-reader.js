import { FoliateReaderAdapter } from './foliate-reader-adapter.js'

const $ = selector => document.querySelector(selector)
const body = document.body
const bookId = body.dataset.bookId
const textIndexReady = body.dataset.textIndexReady === 'true'
const host = $('#chapter-content')
const bookTitle = $('#reader-book-title')
const chapterTitle = $('#reader-chapter-title')
const tocList = $('#toc-list')
const tocDrawer = $('#toc-drawer')
const scrim = $('#drawer-scrim')
const progressBar = $('#reader-progress span')
const modeButton = $('#reading-mode')
const pagePrevious = $('#page-previous')
const pageNext = $('#page-next')
const previousButton = $('#previous-chapter')
const nextButton = $('#next-chapter')
const pageStatus = $('#page-status')
const menu = $('#reader-menu')
const menuButton = $('#reader-menu-button')
const adapter = new FoliateReaderAdapter(host)
const abortController = new AbortController()

let flow = localStorage.getItem('xiaxia-foliate-flow') === 'scrolled'
    ? 'scrolled' : 'paginated'
let fontSize = Number(localStorage.getItem('xiaxia-foliate-font-size')) || 19
let saveTimer = null
let lastLocator = null

const api = async (url, options = {}) => {
    const response = await fetch(url, {
        credentials: 'same-origin',
        cache: 'no-store',
        ...options,
        headers: {
            Accept: 'application/json',
            ...(options.body ? { 'Content-Type': 'application/json' } : {}),
            ...(options.headers || {}),
        },
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.message || data.error || `request_${response.status}`)
    return data
}

const localizedText = value => {
    if (!value) return ''
    if (typeof value === 'string') return value
    return value[Object.keys(value)[0]] || ''
}

const flattenTOC = (items, depth = 0, output = []) => {
    for (const item of items || []) {
        output.push({ ...item, depth })
        flattenTOC(item.subitems || item.children || [], depth + 1, output)
    }
    return output
}

const renderTOC = items => {
    tocList.replaceChildren()
    for (const item of flattenTOC(items)) {
        const button = document.createElement('button')
        button.type = 'button'
        button.textContent = localizedText(item.label) || item.href || '未命名章节'
        button.style.paddingLeft = `${.75 + item.depth * .8}rem`
        button.addEventListener('click', async () => {
            await adapter.goTo(item.href)
            closeTOC()
        })
        tocList.append(button)
    }
}

const closeTOC = () => {
    tocDrawer.classList.remove('open')
    tocDrawer.setAttribute('aria-hidden', 'true')
    scrim.hidden = true
}

const openTOC = () => {
    tocDrawer.classList.add('open')
    tocDrawer.setAttribute('aria-hidden', 'false')
    scrim.hidden = false
}

const applyFlowUI = () => {
    body.classList.toggle('foliate-scroll-mode', flow === 'scrolled')
    body.classList.toggle('foliate-paginated-mode', flow === 'paginated')
    modeButton.textContent = flow === 'scrolled' ? '切换为分页阅读' : '切换为滚动阅读'
    pagePrevious.hidden = flow === 'scrolled'
    pageNext.hidden = flow === 'scrolled'
    adapter.setFlow(flow)
}

const saveProgress = locator => {
    lastLocator = locator
    clearTimeout(saveTimer)
    saveTimer = setTimeout(async () => {
        try {
            await api(`/api/books/${bookId}/publication-progress`, {
                method: 'PUT',
                body: JSON.stringify({
                    locator,
                    progression: locator.progression,
                }),
            })
        } catch (_error) {
            showStatus('阅读位置暂时没有保存')
        }
    }, 650)
}

const showStatus = message => {
    pageStatus.textContent = message
    pageStatus.hidden = false
    setTimeout(() => { pageStatus.hidden = true }, 1800)
}

const fallbackOrShowError = error => {
    if (textIndexReady && new URLSearchParams(location.search).get('engine') !== 'legacy') {
        const fallback = new URL(location.href)
        fallback.searchParams.set('engine', 'legacy')
        fallback.searchParams.set('fallback', 'foliate')
        location.replace(fallback)
        return
    }
    host.innerHTML = `<p class="form-error">原始书页暂时无法打开：${escapeHTML(error.message || error)}</p>`
}

const escapeHTML = value => {
    const node = document.createElement('span')
    node.textContent = String(value || '')
    return node.innerHTML
}

const initialize = async () => {
    try {
        const [{ book }, source] = await Promise.all([
            api(`/api/books/${bookId}`),
            api(`/api/books/${bookId}/source-access`),
        ])
        bookTitle.textContent = book.title
        document.title = `${book.title} · 共读小屋`
        lastLocator = book.publication_progress?.locator || null
        const downloaded = await FoliateReaderAdapter.downloadPublication(
            source.signed_url,
            source.book.filename,
            abortController.signal,
        )
        const publication = await adapter.openPublication(downloaded.file, {
            flow,
            preferences: { fontSize },
            locator: lastLocator,
        })
        const engineTitle = localizedText(publication.metadata?.title)
        if (engineTitle) chapterTitle.textContent = engineTitle
        renderTOC(publication.toc)
        applyFlowUI()
        showStatus('原始 EPUB 已打开')
    } catch (error) {
        fallbackOrShowError(error)
    }
}

adapter.onRelocate(locator => {
    const percentage = Math.max(0, Math.min(100, locator.progression * 100))
    progressBar.style.width = `${percentage}%`
    $('#chapter-position').textContent = `${percentage.toFixed(1)}%`
    saveProgress(locator)
})

adapter.onSelection(() => {
    showStatus('划线与留句将在下一阶段接回共同书页')
})

adapter.onError(error => console.error('foliate-reader', error))

pagePrevious.addEventListener('click', () => adapter.previous())
pageNext.addEventListener('click', () => adapter.next())
previousButton.addEventListener('click', () => adapter.previous())
nextButton.addEventListener('click', () => adapter.next())
$('#toc-button').addEventListener('click', openTOC)
$('#toc-close').addEventListener('click', closeTOC)
scrim.addEventListener('click', closeTOC)

menuButton.addEventListener('click', () => {
    menu.hidden = !menu.hidden
    menuButton.setAttribute('aria-expanded', String(!menu.hidden))
})

modeButton.addEventListener('click', () => {
    flow = flow === 'scrolled' ? 'paginated' : 'scrolled'
    localStorage.setItem('xiaxia-foliate-flow', flow)
    applyFlowUI()
    menu.hidden = true
})

$('#font-down').addEventListener('click', () => {
    fontSize = Math.max(12, fontSize - 2)
    localStorage.setItem('xiaxia-foliate-font-size', String(fontSize))
    adapter.setPreferences({ fontSize })
})

$('#font-up').addEventListener('click', () => {
    fontSize = Math.min(36, fontSize + 2)
    localStorage.setItem('xiaxia-foliate-font-size', String(fontSize))
    adapter.setPreferences({ fontSize })
})

addEventListener('pagehide', () => {
    if (lastLocator) fetch(`/api/books/${bookId}/publication-progress`, {
        method: 'PUT',
        credentials: 'same-origin',
        keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            locator: lastLocator,
            progression: lastLocator.progression,
        }),
    }).catch(() => {})
    abortController.abort()
    adapter.closePublication()
})

initialize()
