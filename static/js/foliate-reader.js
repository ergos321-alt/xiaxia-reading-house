import { FoliateReaderAdapter } from './foliate-reader-adapter.js'

const $ = selector => document.querySelector(selector)
const body = document.body
const bookId = body.dataset.bookId
const textIndexReady = body.dataset.textIndexReady === 'true'
const dualAnchorFeature = body.dataset.dualAnchorEnabled === 'true'
const host = $('#chapter-content')
const adapter = new FoliateReaderAdapter(host)
const abortController = new AbortController()
const selectionMenu = $('#selection-menu')
const noteDialog = $('#note-dialog')
const annotationDialog = $('#annotation-dialog')
const noteText = $('#note-text')
const readonlyNote = $('#foliate-readonly-note')

let flow = localStorage.getItem('xiaxia-foliate-flow') === 'scrolled' ? 'scrolled' : 'paginated'
let fontSize = Number(localStorage.getItem('xiaxia-foliate-font-size')) || 19
let saveTimer = null
let lastLocator = null
let book = null
let source = null
let bridgeReady = false
let savedSelection = null
let selectionEpoch = 0
let selectionSuppressedUntil = 0
let selectionSubmitting = false
let annotations = []
let thoughts = []
let openRecord = null
let editingAnnotationId = null
let tracePoll = null

const api = async (url, options = {}) => {
    const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', ...options,
        headers: {
            Accept: 'application/json',
            ...(options.body ? { 'Content-Type': 'application/json' } : {}),
            ...(options.headers || {}),
        },
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
        const error = new Error(data.message || data.error || `request_${response.status}`)
        error.code = data.error
        throw error
    }
    return data
}

const localizedText = value => typeof value === 'string'
    ? value : value?.[Object.keys(value || {})[0]] || ''

const showStatus = message => {
    const node = $('#page-status')
    node.textContent = message
    node.hidden = false
    setTimeout(() => { node.hidden = true }, 2200)
}

const hideSelectionMenu = clear => {
    selectionMenu.hidden = true
    selectionMenu.classList.add('is-hidden')
    selectionMenu.setAttribute('aria-hidden', 'true')
    selectionMenu.style.setProperty('display', 'none', 'important')
    if (clear) savedSelection = null
}

const clearSelectionInteraction = () => {
    selectionEpoch += 1
    selectionSuppressedUntil = Date.now() + 900
    savedSelection = null
    selectionSubmitting = false
    adapter.clearBrowserSelection()
    hideSelectionMenu(false)
}

const showSelectionMenu = () => {
    if (!bridgeReady || !savedSelection || Date.now() < selectionSuppressedUntil) return
    selectionMenu.classList.remove('is-hidden')
    selectionMenu.style.removeProperty('display')
    selectionMenu.hidden = false
    selectionMenu.setAttribute('aria-hidden', 'false')
    selectionMenu.dataset.placement = 'mobile-clamped'
}

const flattenTOC = (items, depth = 0, output = []) => {
    for (const item of items || []) {
        output.push({ ...item, depth })
        flattenTOC(item.subitems || item.children || [], depth + 1, output)
    }
    return output
}

const closeTOC = () => {
    $('#toc-drawer').classList.remove('open')
    $('#toc-drawer').setAttribute('aria-hidden', 'true')
    $('#drawer-scrim').hidden = true
}

const renderTOC = items => {
    const list = $('#toc-list')
    list.replaceChildren()
    for (const item of flattenTOC(items)) {
        const button = document.createElement('button')
        button.type = 'button'
        button.textContent = localizedText(item.label) || item.href || '未命名章节'
        button.style.paddingLeft = `${.75 + item.depth * .8}rem`
        button.addEventListener('click', async () => {
            await adapter.goTo(item.href)
            closeTOC()
        })
        list.append(button)
    }
}

const applyFlowUI = () => {
    body.classList.toggle('foliate-scroll-mode', flow === 'scrolled')
    body.classList.toggle('foliate-paginated-mode', flow === 'paginated')
    $('#reading-mode').textContent = flow === 'scrolled' ? '切换为分页阅读' : '切换为滚动阅读'
    $('#page-previous').hidden = flow === 'scrolled'
    $('#page-next').hidden = flow === 'scrolled'
    adapter.setFlow(flow)
}

const saveProgress = locator => {
    lastLocator = locator
    clearTimeout(saveTimer)
    saveTimer = setTimeout(() => api(`/api/books/${bookId}/publication-progress`, {
        method: 'PUT', body: JSON.stringify({ locator, progression: locator.progression }),
    }).catch(() => showStatus('阅读位置暂时没有保存')), 650)
}

const validLocator = locator => locator
    && locator.engine === 'foliate-js'
    && locator.engine_adapter_version === 1
    && locator.bridge_version === 1
    && locator.source_sha256 === source?.book?.source_sha256
    && String(locator.cfi || '').startsWith('epubcfi(')

const recordById = (kind, id) => (kind === 'xiaxia' ? thoughts : annotations)
    .find(item => String(item.id) === String(id))

const addTraceDecoration = async (record, kind) => {
    if (validLocator(record.engine_locator)) {
        await adapter.addDecoration({ ...record, kind })
        return
    }
    if (!record.locator_seed || !bridgeReady) return
    try {
        const engineLocator = await adapter.locatorFromSeed(record.locator_seed)
        const result = await api(
            `/api/books/${bookId}/engine-traces/${kind === 'xiaxia' ? 'thought' : 'annotation'}/${record.id}/locator`,
            { method: 'PUT', body: JSON.stringify({ engine_locator: engineLocator }) },
        )
        record.engine_locator = result.record.engine_locator
        record.engine_locator_version = result.record.engine_locator_version
        await adapter.addDecoration({ ...record, kind })
    } catch (error) {
        console.info('locator backfill skipped', record.id, error.code || error.message)
    }
}

const refreshTraces = async () => {
    if (!bridgeReady) return
    const data = await api(`/api/books/${bookId}/engine-traces`)
    annotations = data.annotations || []
    thoughts = data.xiaxia_thoughts || []
    for (const item of annotations) await addTraceDecoration(item, 'user')
    for (const item of thoughts.filter(item => item.scope !== 'chapter')) {
        await addTraceDecoration(item, 'xiaxia')
    }
    renderChapterThoughtMarker(lastLocator?.href)
}

const renderChapterThoughtMarker = href => {
    const panel = $('#chapter-thoughts')
    const current = thoughts.filter(item => item.scope === 'chapter' && item.chapter_href === href)
    panel.replaceChildren()
    panel.hidden = current.length === 0
    for (const thought of current) {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'chapter-thought'
        button.innerHTML = '<span class="thought-paw" aria-hidden="true">🐾</span><span><strong>林知夏在这一章停留过</strong><small>点开页边留下的话</small></span>'
        button.addEventListener('click', () => openTrace('xiaxia', thought.id))
        panel.append(button)
    }
}

const saveAnnotation = async comment => {
    if (!savedSelection || selectionSubmitting) return false
    const snapshot = structuredClone(savedSelection)
    selectionSubmitting = true
    hideSelectionMenu(false)
    try {
        const { annotation } = await api('/api/annotations', {
            method: 'POST', body: JSON.stringify({ book_id: bookId, engine_locator: snapshot, comment }),
        })
        annotations.push(annotation)
        await adapter.addDecoration({ ...annotation, kind: 'user' })
        clearSelectionInteraction()
        showStatus(comment ? '批注已经留在书页旁。' : '划线已经保存。')
        return true
    } catch (error) {
        selectionSubmitting = false
        showStatus(error.code?.startsWith('locator_')
            ? '这处文字暂时无法留下共读痕迹。' : `保存失败：${error.message}`)
        return false
    }
}

const openNewNote = () => {
    if (!savedSelection) return
    editingAnnotationId = null
    $('#note-dialog-title').textContent = '写在书页旁'
    $('#selected-quote').textContent = savedSelection.text.highlight
    noteText.value = ''
    hideSelectionMenu(false)
    noteDialog.showModal()
    setTimeout(() => noteText.focus(), 0)
}

const openTrace = (kind, id) => {
    const record = recordById(kind, id)
    if (!record) return
    openRecord = { kind, id: String(id) }
    $('#annotation-kind').textContent = kind === 'user' ? '我的划线 / 批注' : '林知夏的独立想法'
    $('#annotation-quote').textContent = record.selected_text || '（本章）'
    $('#user-note-section').hidden = kind !== 'user'
    $('#annotation-user-note').textContent = record.comment || '（只留下了划线）'
    $('#xiaxia-reply-section').hidden = kind !== 'user' || !record.xiaxia_response
    $('#annotation-xiaxia-reply').textContent = record.xiaxia_response || ''
    $('#xiaxia-thought-section').hidden = kind !== 'xiaxia'
    $('#annotation-xiaxia-thought').textContent = record.content || ''
    $('#thought-user-reply-section').hidden = kind !== 'xiaxia'
    $('#thought-user-reply').textContent = record.user_response || '（我还没有回复）'
    $('#thought-actions').hidden = kind !== 'xiaxia'
    $('#annotation-actions').hidden = kind !== 'user'
    $('#thought-reply-delete').hidden = !record.user_response
    $('#thought-reply-edit').textContent = record.user_response ? '编辑我的回复' : '回复林知夏'
    $('#trace-actions-menu').hidden = true
    if (!annotationDialog.open) annotationDialog.showModal()
}

const editCurrentAnnotation = () => {
    const record = openRecord?.kind === 'user' && recordById('user', openRecord.id)
    if (!record) return
    editingAnnotationId = record.id
    $('#note-dialog-title').textContent = record.comment ? '编辑我的想法' : '给这条划线添加想法'
    $('#selected-quote').textContent = record.selected_text
    noteText.value = record.comment || ''
    annotationDialog.close()
    noteDialog.showModal()
}

const deleteCurrentAnnotation = async () => {
    const record = openRecord?.kind === 'user' && recordById('user', openRecord.id)
    if (!record || !confirm('确定删除这条划线 / 批注吗？')) return
    await api(`/api/annotations/${record.id}`, { method: 'DELETE' })
    annotations = annotations.filter(item => item.id !== record.id)
    await adapter.removeDecoration(record.id)
    annotationDialog.close()
    showStatus('划线与批注已删除。')
}

const writeThoughtReply = async () => {
    const thought = openRecord?.kind === 'xiaxia' && recordById('xiaxia', openRecord.id)
    if (!thought) return
    const response = prompt('回复林知夏的这条想法', thought.user_response || '')
    if (!response?.trim()) return
    const data = await api(`/api/xiaxia/thoughts/${thought.id}/reply`, {
        method: thought.user_response ? 'PATCH' : 'POST',
        body: JSON.stringify({ response: response.trim() }),
    })
    thought.user_response = data.user_reply.response
    openTrace('xiaxia', thought.id)
}

const deleteThoughtReply = async () => {
    const thought = openRecord?.kind === 'xiaxia' && recordById('xiaxia', openRecord.id)
    if (!thought?.user_response || !confirm('确定删除这条回复吗？')) return
    await api(`/api/xiaxia/thoughts/${thought.id}/reply`, { method: 'DELETE' })
    thought.user_response = null
    openTrace('xiaxia', thought.id)
}

const fallbackOrShowError = error => {
    if (textIndexReady && new URLSearchParams(location.search).get('engine') !== 'legacy') {
        const fallback = new URL(location.href)
        fallback.searchParams.set('engine', 'legacy')
        fallback.searchParams.set('fallback', 'foliate')
        location.replace(fallback)
        return
    }
    host.textContent = `原始书页暂时无法打开：${error.message || error}`
}

const initialize = async () => {
    try {
        const results = await Promise.all([
            api(`/api/books/${bookId}`), api(`/api/books/${bookId}/source-access`),
        ])
        book = results[0].book
        source = results[1]
        bridgeReady = dualAnchorFeature && book.locator_bridge_status === 'ready'
        body.classList.toggle('foliate-dual-anchor', bridgeReady)
        readonlyNote.hidden = bridgeReady
        if (dualAnchorFeature && textIndexReady && book.locator_bridge_status !== 'ready') {
            readonlyNote.textContent = '新书页可阅读；共读书页仍在准备'
            api(`/api/books/${bookId}/locator-bridge`, { method: 'POST' })
                .then(() => location.reload()).catch(() => {})
        }
        $('#reader-book-title').textContent = book.title
        document.title = `${book.title} · 共读小屋`
        lastLocator = book.publication_progress?.locator || null
        const downloaded = await FoliateReaderAdapter.downloadPublication(
            source.signed_url, source.book.filename, abortController.signal)
        const publication = await adapter.openPublication(downloaded.file, {
            flow, preferences: { fontSize }, locator: lastLocator,
        })
        $('#reader-chapter-title').textContent = localizedText(publication.metadata?.title) || ''
        renderTOC(publication.toc)
        applyFlowUI()
        if (bridgeReady) {
            await refreshTraces()
            tracePoll = setInterval(() => refreshTraces().catch(() => {}), 30000)
        }
        showStatus('原始 EPUB 已打开')
    } catch (error) {
        fallbackOrShowError(error)
    }
}

adapter.onRelocate(locator => {
    if (source?.book?.source_sha256) locator = {
        ...locator,
        source_sha256: source.book.source_sha256,
        engine_adapter_version: 1,
        bridge_version: book?.locator_bridge_version || 1,
    }
    const percentage = Math.max(0, Math.min(100, locator.progression * 100))
    $('#reader-progress span').style.width = `${percentage}%`
    $('#chapter-position').textContent = `${percentage.toFixed(1)}%`
    saveProgress(locator)
    renderChapterThoughtMarker(locator.href)
    $('#finish-book-entry').hidden = locator.progression < .995
})

adapter.onSelection(detail => {
    if (!bridgeReady || selectionSubmitting || Date.now() < selectionSuppressedUntil) return
    const epoch = ++selectionEpoch
    savedSelection = {
        engine: 'foliate-js', engine_adapter_version: 1, bridge_version: 1,
        source_sha256: source.book.source_sha256,
        href: detail.href, spine_index: detail.section_index,
        cfi: detail.cfi, progression: detail.progression, text: detail.text,
    }
    setTimeout(() => { if (epoch === selectionEpoch) showSelectionMenu() }, 40)
})

adapter.onAnnotation(({ records }) => {
    const record = records[records.length - 1]
    if (record) openTrace(record.kind, record.id)
})
adapter.onError(error => console.error('foliate-reader', error))

$('#highlight-selection').addEventListener('click', () => saveAnnotation(''))
$('#note-selection').addEventListener('click', openNewNote)
$('#note-form').addEventListener('submit', async event => {
    event.preventDefault()
    if (editingAnnotationId) {
        const data = await api(`/api/annotations/${editingAnnotationId}`, {
            method: 'PATCH', body: JSON.stringify({ comment: noteText.value.trim() }),
        })
        const index = annotations.findIndex(item => item.id === editingAnnotationId)
        if (index >= 0) annotations[index] = { ...annotations[index], ...data.annotation }
        noteDialog.close()
        editingAnnotationId = null
        showStatus('批注已经更新。')
    } else if (await saveAnnotation(noteText.value.trim())) noteDialog.close()
})
document.querySelectorAll('[data-close-note]').forEach(button => button.addEventListener('click', () => {
    noteDialog.close()
    if (!editingAnnotationId) clearSelectionInteraction()
    editingAnnotationId = null
}))

$('#trace-menu-button').addEventListener('click', () => {
    $('#trace-actions-menu').hidden = !$('#trace-actions-menu').hidden
})
$('#edit-annotation').addEventListener('click', editCurrentAnnotation)
$('#delete-annotation').addEventListener('click', () => deleteCurrentAnnotation().catch(error => showStatus(error.message)))
$('#thought-reply-edit').addEventListener('click', () => writeThoughtReply().catch(error => showStatus(error.message)))
$('#thought-reply-delete').addEventListener('click', () => deleteThoughtReply().catch(error => showStatus(error.message)))

$('#page-previous').addEventListener('click', () => adapter.previous())
$('#page-next').addEventListener('click', () => adapter.next())
$('#previous-chapter').addEventListener('click', () => adapter.previous())
$('#next-chapter').addEventListener('click', () => adapter.next())
$('#toc-button').addEventListener('click', () => {
    $('#toc-drawer').classList.add('open')
    $('#toc-drawer').setAttribute('aria-hidden', 'false')
    $('#drawer-scrim').hidden = false
})
$('#toc-close').addEventListener('click', closeTOC)
$('#drawer-scrim').addEventListener('click', closeTOC)
$('#reader-menu-button').addEventListener('click', () => {
    $('#reader-menu').hidden = !$('#reader-menu').hidden
})
$('#reading-mode').addEventListener('click', () => {
    flow = flow === 'scrolled' ? 'paginated' : 'scrolled'
    localStorage.setItem('xiaxia-foliate-flow', flow)
    applyFlowUI()
    $('#reader-menu').hidden = true
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
$('#finish-book-button').addEventListener('click', async () => {
    const button = $('#finish-book-button')
    button.disabled = true
    try {
        if (lastLocator) await api(`/api/books/${bookId}/publication-progress`, {
            method: 'PUT', body: JSON.stringify({ locator: lastLocator, progression: 1 }),
        })
        await api(`/api/books/${bookId}/completion`, { method: 'POST' })
        location.assign(`/reader/${bookId}/after-reading`)
    } catch (error) {
        button.disabled = false
        showStatus(`暂时无法合上正文：${error.message}`)
    }
})

addEventListener('pagehide', () => {
    clearInterval(tracePoll)
    if (lastLocator) fetch(`/api/books/${bookId}/publication-progress`, {
        method: 'PUT', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ locator: lastLocator, progression: lastLocator.progression }),
    }).catch(() => {})
    abortController.abort()
    adapter.closePublication()
})

initialize()
