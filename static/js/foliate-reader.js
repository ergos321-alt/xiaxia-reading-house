import { FoliateReaderAdapter } from './foliate-reader-adapter.js'
import { canonicalHref, normalizeTextV1 } from './canonical-text.js'

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
let pendingSelectionSnapshot = null
let selectionEpoch = 0
let selectionSuppressedUntil = 0
let selectionSubmitting = false
let annotations = []
let thoughts = []
let openRecord = null
let editingAnnotationId = null
let tracePoll = null
let traceRefreshPromise = null
let traceRevalidated = false
let initialTraceHandled = false
const selectionDebugEnabled = new URLSearchParams(location.search).get('selection_debug') === '1'
let selectionDebugState = { state: 'idle', save_stage: 'idle' }

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
        error.status = response.status
        error.detail = data
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

const cfiFingerprint = value => {
    const text = String(value || '')
    let hash = 2166136261
    for (let index = 0; index < text.length; index += 1) {
        hash ^= text.charCodeAt(index)
        hash = Math.imul(hash, 16777619)
    }
    return text ? `${text.slice(0, 30)}… #${(hash >>> 0).toString(16).padStart(8, '0')}` : ''
}

const updateSelectionDebug = (stage, detail = {}) => {
    if (!selectionDebugEnabled) return
    selectionDebugState = {
        ...selectionDebugState,
        ...detail,
        state: detail.state || selectionDebugState.state,
        last_stage: stage,
        updated_at: new Date().toISOString(),
    }
    const panel = $('#selection-diagnostics')
    if (!panel) return
    const output = panel.querySelector('pre')
    output.textContent = JSON.stringify({
        ...selectionDebugState,
        cfi: cfiFingerprint(selectionDebugState.cfi),
        page_before_cfi: cfiFingerprint(selectionDebugState.page_before?.cfi),
        page_after_cfi: cfiFingerprint(selectionDebugState.page_after?.cfi),
    }, null, 2)
}

const installSelectionDiagnostics = () => {
    if (!selectionDebugEnabled) return
    const panel = document.createElement('aside')
    panel.id = 'selection-diagnostics'
    panel.className = 'selection-diagnostics'
    panel.innerHTML = `
        <div><strong>Selection diagnostics</strong><button type="button" data-close>×</button></div>
        <pre></pre>
        <div class="selection-diagnostics-actions">
          <button type="button" data-copy>复制诊断 JSON</button>
          <button type="button" data-rebuild>重验本书定位</button>
        </div>`
    panel.querySelector('[data-close]').addEventListener('click', () => panel.remove())
    panel.querySelector('[data-copy]').addEventListener('click', async () => {
        await navigator.clipboard?.writeText?.(JSON.stringify(selectionDebugState, null, 2))
        showStatus('诊断信息已复制。')
    })
    panel.querySelector('[data-rebuild]').addEventListener('click', async () => {
        if (!confirm('只清空并按需重建本书的 renderer locator？原有批注、Thought 和 legacy anchor 都会保留。')) return
        await api(`/api/books/${bookId}/engine-traces/revalidate`, {
            method: 'POST', body: JSON.stringify({ rebuild_all: true }),
        })
        location.reload()
    })
    body.append(panel)
    globalThis.__readingHouseSelectionDebug = {
        adapter,
        getState: () => ({
            ...selectionDebugState,
            saved_selection: savedSelection && structuredClone(savedSelection),
            pending_selection: pendingSelectionSnapshot && structuredClone(pendingSelectionSnapshot),
            adapter: adapter.getSelectionDiagnostics(),
        }),
        refreshTraces: () => refreshTraces(),
        navigateToTrace: (kind, id) => navigateToTrace(kind, id),
    }
    updateSelectionDebug('diagnostics_ready')
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
    pendingSelectionSnapshot = null
    selectionSubmitting = false
    adapter.clearBrowserSelection()
    hideSelectionMenu(false)
}

const freezeSelectionForModal = () => {
    if (!savedSelection) return null
    pendingSelectionSnapshot = structuredClone(savedSelection)
    selectionEpoch += 1
    selectionSuppressedUntil = Date.now() + 900
    savedSelection = null
    adapter.clearBrowserSelection()
    hideSelectionMenu(false)
    updateSelectionDebug('modal_snapshot_frozen', {
        state: 'snapshot_frozen',
        selected_text: pendingSelectionSnapshot.text?.highlight || '',
        selected_text_length: pendingSelectionSnapshot.text?.highlight?.length || 0,
        href: pendingSelectionSnapshot.href,
        start_section: pendingSelectionSnapshot.spine_index,
        end_section: pendingSelectionSnapshot.spine_index,
        cfi: pendingSelectionSnapshot.cfi,
    })
    return pendingSelectionSnapshot
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

const validLocator = (locator, record) => locator
    && locator.engine === 'foliate-js'
    && locator.engine_adapter_version === 1
    && locator.bridge_version === 1
    && locator.source_sha256 === source?.book?.source_sha256
    && String(locator.cfi || '').startsWith('epubcfi(')
    && locator.locator_integrity_version === 1
    && canonicalHref(locator.browser_truth?.href) === canonicalHref(locator.href)
    && Number(locator.browser_truth?.section_index) === Number(locator.spine_index)
    && normalizeTextV1(locator.browser_truth?.text) === normalizeTextV1(record?.selected_text)

const recordById = (kind, id) => (kind === 'xiaxia' ? thoughts : annotations)
    .find(item => String(item.id) === String(id))

const traceType = kind => kind === 'xiaxia' ? 'thought' : 'annotation'

const persistRecordLocator = async (record, kind, engineLocator) => {
    const result = await api(
        `/api/books/${bookId}/engine-traces/${traceType(kind)}/${record.id}/locator`,
        { method: 'PUT', body: JSON.stringify({ engine_locator: engineLocator }) },
    )
    record.engine_locator = result.record.engine_locator
    record.engine_locator_version = result.record.engine_locator_version
    record.engine_anchor_verified_at = result.record.engine_anchor_verified_at
    return record.engine_locator
}

const invalidateRecordLocator = async (record, kind) => {
    await api(
        `/api/books/${bookId}/engine-traces/${traceType(kind)}/${record.id}/locator`,
        { method: 'DELETE' },
    )
    record.engine_locator = null
    record.engine_locator_version = null
    record.engine_anchor_verified_at = null
    await adapter.removeDecoration(traceType(kind), record.id)
}

const ensureRecordLocator = async (record, kind, { navigate = false } = {}) => {
    if (validLocator(record.engine_locator, record)) {
        try {
            await adapter.verifyLocator(record.engine_locator, record.selected_text, { navigate })
            return record.engine_locator
        } catch (error) {
            if (error.code !== 'locator_mapping_missing' || navigate) {
                await invalidateRecordLocator(record, kind)
            } else {
                throw error
            }
        }
    }
    if (!record.locator_seed || !bridgeReady) {
        const error = new Error('locator_mapping_missing')
        error.code = 'locator_mapping_missing'
        throw error
    }
    const engineLocator = await adapter.locatorFromSeed(record.locator_seed, { navigate })
    return persistRecordLocator(record, kind, engineLocator)
}

const addTraceDecoration = async (record, kind) => {
    try {
        await ensureRecordLocator(record, kind)
        const drawn = await adapter.addDecoration({ ...record, kind })
        if (!drawn) throw Object.assign(new Error('locator_mapping_missing'), {
            code: 'locator_mapping_missing',
        })
    } catch (error) {
        console.info('locator backfill skipped', record.id, error.code || error.message)
    }
}

const refreshTraces = async () => {
    if (!bridgeReady) return
    if (traceRefreshPromise) return traceRefreshPromise
    traceRefreshPromise = (async () => {
        if (!traceRevalidated) {
            await api(`/api/books/${bookId}/engine-traces/revalidate`, {
                method: 'POST', body: JSON.stringify({}),
            })
            traceRevalidated = true
        }
        const data = await api(`/api/books/${bookId}/engine-traces`)
        annotations = data.annotations || []
        thoughts = data.xiaxia_thoughts || []
        for (const item of annotations) await addTraceDecoration(item, 'user')
        for (const item of thoughts.filter(item => item.scope !== 'chapter')) {
            await addTraceDecoration(item, 'xiaxia')
        }
        renderChapterThoughtMarker(lastLocator?.href)
    })()
    try { await traceRefreshPromise } finally { traceRefreshPromise = null }
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

const saveAnnotation = async (comment, candidate = savedSelection) => {
    if (!candidate || selectionSubmitting) return false
    const snapshot = structuredClone(candidate)
    selectionSubmitting = true
    hideSelectionMenu(false)
    updateSelectionDebug('save_handler', {
        save_stage: 'handler',
        selected_text_length: snapshot.text?.highlight?.length || 0,
        href: snapshot.href,
        start_section: snapshot.spine_index,
        end_section: snapshot.spine_index,
        cfi: snapshot.cfi,
        comment_length: comment.length,
    })
    let annotation
    try {
        updateSelectionDebug('save_post_started', { save_stage: 'post_started' })
        const result = await api('/api/annotations', {
            method: 'POST', body: JSON.stringify({ book_id: bookId, engine_locator: snapshot, comment }),
        })
        annotation = result.annotation
    } catch (error) {
        selectionSubmitting = false
        updateSelectionDebug('save_post_failed', {
            save_stage: 'post_failed', error_code: error.code || 'request_failed',
            http_status: error.status || null,
        })
        console.info('reading_house_trace_write', {
            stage: 'post_failed', book_id: bookId, record_type: 'annotation',
            href: snapshot.href, section_index: snapshot.spine_index,
            selected_text_length: snapshot.text?.highlight?.length || 0,
            cfi: cfiFingerprint(snapshot.cfi), error_code: error.code || 'request_failed',
        })
        showStatus(error.code?.startsWith('locator_')
            ? '这处文字暂时无法留下共读痕迹。' : `保存失败：${error.message}`)
        if (savedSelection && !noteDialog.open) setTimeout(showSelectionMenu, 40)
        return false
    }
    const existing = annotations.findIndex(item => String(item.id) === String(annotation.id))
    if (existing >= 0) annotations[existing] = annotation
    else annotations.push(annotation)
    selectionSubmitting = false
    clearSelectionInteraction()
    updateSelectionDebug('save_persisted', {
        save_stage: 'persisted', record_id: annotation.id, error_code: null,
    })
    let drawn = false
    try {
        drawn = await adapter.addDecoration({ ...annotation, kind: 'user' })
    } catch (error) {
        console.info('reading_house_trace_write', {
            stage: 'decoration_deferred', book_id: bookId,
            record_type: 'annotation', record_id: annotation.id,
            href: snapshot.href, section_index: snapshot.spine_index,
            selected_text_length: annotation.selected_text?.length || 0,
            cfi: cfiFingerprint(annotation.engine_locator?.cfi),
            error_code: error.code || 'decoration_failed',
        })
    }
    updateSelectionDebug('save_complete', {
        save_stage: drawn ? 'decoration_drawn' : 'decoration_deferred',
        decoration_drawn: Boolean(drawn),
    })
    if (!drawn) setTimeout(() => refreshTraces().catch(() => {}), 80)
    showStatus(comment ? '批注已经留在书页旁。' : '划线已经保存。')
    return true
}

const openNewNote = () => {
    if (!savedSelection) return
    const snapshot = freezeSelectionForModal()
    if (!snapshot) return
    editingAnnotationId = null
    $('#note-dialog-title').textContent = '写在书页旁'
    $('#selected-quote').textContent = snapshot.text.highlight
    noteText.value = ''
    noteDialog.showModal()
    setTimeout(() => noteText.focus(), 0)
}

const openTrace = (kind, id) => {
    const record = recordById(kind, id)
    if (!record) return
    openRecord = { kind, id: String(id) }
    $('#trace-record-choices').hidden = true
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

const openTraceChoices = records => {
    const choices = $('#trace-record-choices')
    choices.replaceChildren()
    choices.hidden = false
    $('#annotation-kind').textContent = '这处书页有多条共读痕迹'
    $('#annotation-quote').textContent = records[0]?.expectedText || ''
    for (const record of records) {
        const sourceRecord = recordById(record.kind, record.id)
        const summary = String(sourceRecord?.content || sourceRecord?.comment || '只留下了划线')
            .replace(/\s+/g, ' ').slice(0, 36)
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'secondary-button'
        button.textContent = `${record.kind === 'xiaxia' ? '林知夏' : '我'} · ${summary}`
        button.dataset.recordKey = record.recordKey
        button.addEventListener('click', () => openTrace(record.kind, record.id))
        choices.append(button)
    }
    for (const id of [
        'user-note-section', 'xiaxia-reply-section', 'xiaxia-thought-section',
        'thought-user-reply-section', 'thought-actions', 'annotation-actions',
    ]) $(`#${id}`).hidden = true
    $('#trace-actions-menu').hidden = true
    if (!annotationDialog.open) annotationDialog.showModal()
}

const navigateToTrace = async (kind, id, { open = true } = {}) => {
    const record = recordById(kind, id)
    if (!record || !record.start_block_id) throw new Error('locator_mapping_missing')
    try {
        const locator = await ensureRecordLocator(record, kind, { navigate: true })
        await adapter.addDecoration({ ...record, kind })
        if (open) openTrace(kind, id)
        return true
    } catch (error) {
        showStatus('这条痕迹暂时无法准确定位，已停止跳转。')
        console.info('record navigation failed closed', traceType(kind), id, error.code || error.message)
        return false
    }
}

const handleInitialTraceTarget = async () => {
    if (initialTraceHandled) return
    initialTraceHandled = true
    const query = new URLSearchParams(location.search)
    if (query.get('annotation_id')) {
        await navigateToTrace('user', query.get('annotation_id'))
    } else if (query.get('thought_id')) {
        await navigateToTrace('xiaxia', query.get('thought_id'))
    }
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
    await adapter.removeDecoration('annotation', record.id)
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
            await handleInitialTraceTarget()
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
    if (!bridgeReady || selectionSubmitting || noteDialog.open || Date.now() < selectionSuppressedUntil) return
    if (detail.selection_state !== 'stable'
        || Number(detail.start_section_index) !== Number(detail.end_section_index)) {
        showStatus('选区跨过了书页边界，请重新选择。')
        return
    }
    const epoch = ++selectionEpoch
    savedSelection = {
        engine: 'foliate-js', engine_adapter_version: 1, bridge_version: 1,
        source_sha256: source.book.source_sha256,
        href: detail.href, spine_index: detail.section_index,
        cfi: detail.cfi, progression: detail.progression, text: detail.text,
        locator_integrity_version: detail.locator_integrity_version,
        browser_truth: detail.browser_truth,
    }
    updateSelectionDebug('selection_stable', {
        state: 'stable',
        selected_text: detail.text.highlight,
        selected_text_length: detail.text.highlight.length,
        href: detail.href,
        start_section: detail.start_section_index,
        end_section: detail.end_section_index,
        cfi: detail.cfi,
        page_before: detail.page_before,
        page_after: detail.page_after,
    })
    setTimeout(() => { if (epoch === selectionEpoch) showSelectionMenu() }, 40)
})

adapter.onSelectionState(detail => {
    updateSelectionDebug('selection_state', detail)
    if (detail.state === 'rejected') {
        savedSelection = null
        hideSelectionMenu(false)
        showStatus(detail.error === 'selection_page_moved'
            ? '分页选区发生了移动，已恢复书页，请重新选择。'
            : '这次选区没有稳定下来，请重新选择。')
    } else if (detail.state === 'cleared' && !noteDialog.open && !selectionSubmitting) {
        savedSelection = null
        hideSelectionMenu(false)
    }
})

adapter.onAnnotation(({ records }) => {
    if (records.length === 1) openTrace(records[0].kind, records[0].id)
    else if (records.length > 1) openTraceChoices(records)
})
adapter.onLocatorInvalid(({ record }) => {
    const sourceRecord = recordById(record.kind, record.id)
    if (sourceRecord) invalidateRecordLocator(sourceRecord, record.kind).catch(() => {})
})
adapter.onSectionLoad(() => {
    if (bridgeReady) setTimeout(() => refreshTraces().catch(() => {}), 40)
})
adapter.onError(error => console.error('foliate-reader', error))

$('#highlight-selection').addEventListener('click', () => {
    const snapshot = savedSelection && structuredClone(savedSelection)
    saveAnnotation('', snapshot)
})
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
    } else if (await saveAnnotation(noteText.value.trim(), pendingSelectionSnapshot)) {
        noteDialog.close()
    }
})
document.querySelectorAll('[data-close-note]').forEach(button => button.addEventListener('click', () => {
    noteDialog.close()
    if (!editingAnnotationId) clearSelectionInteraction()
    editingAnnotationId = null
}))
noteDialog.addEventListener('cancel', () => {
    if (!editingAnnotationId) clearSelectionInteraction()
    editingAnnotationId = null
})

$('#trace-menu-button').addEventListener('click', () => {
    $('#trace-actions-menu').hidden = !$('#trace-actions-menu').hidden
})
$('#edit-annotation').addEventListener('click', editCurrentAnnotation)
$('#delete-annotation').addEventListener('click', () => deleteCurrentAnnotation().catch(error => showStatus(error.message)))
$('#thought-reply-edit').addEventListener('click', () => writeThoughtReply().catch(error => showStatus(error.message)))
$('#thought-reply-delete').addEventListener('click', () => deleteThoughtReply().catch(error => showStatus(error.message)))

$('#page-previous').addEventListener('click', () => {
    clearSelectionInteraction()
    adapter.previous()
})
$('#page-next').addEventListener('click', () => {
    clearSelectionInteraction()
    adapter.next()
})
$('#previous-chapter').addEventListener('click', () => {
    clearSelectionInteraction()
    adapter.previous()
})
$('#next-chapter').addEventListener('click', () => {
    clearSelectionInteraction()
    adapter.next()
})
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
    clearSelectionInteraction()
    flow = flow === 'scrolled' ? 'paginated' : 'scrolled'
    localStorage.setItem('xiaxia-foliate-flow', flow)
    applyFlowUI()
    $('#reader-menu').hidden = true
})
$('#font-down').addEventListener('click', () => {
    clearSelectionInteraction()
    fontSize = Math.max(12, fontSize - 2)
    localStorage.setItem('xiaxia-foliate-font-size', String(fontSize))
    adapter.setPreferences({ fontSize })
})
$('#font-up').addEventListener('click', () => {
    clearSelectionInteraction()
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

installSelectionDiagnostics()
initialize()
