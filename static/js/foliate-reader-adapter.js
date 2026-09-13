import {
    attachSafeLinkPolicy,
    closePublication as closeSecurePublication,
    createSecurePublication,
    downloadAsFile,
} from './foliate-poc-adapter.js'
import { Overlayer } from '../vendor/foliate-js/overlayer.js'
import {
    canonicalHref,
    normalizeTextV1,
    rangeFromCanonicalOffsets,
    selectionContext,
    textFromRange,
} from './canonical-text.js'

/** Stable Reading House boundary around the pinned foliate-js internals. */
export class FoliateReaderAdapter {
    constructor(host) {
        this.host = host
        this.book = null
        this.view = null
        this.flow = 'paginated'
        this.preferences = { fontSize: 19 }
        this.listeners = {
            relocate: new Set(),
            link: new Set(),
            selection: new Set(),
            selectionState: new Set(),
            annotation: new Set(),
            sectionLoad: new Set(),
            locatorInvalid: new Set(),
            error: new Set(),
        }
        this.decorations = new Map()
        this.selectionSession = null
        this.selectionSerial = 0
    }

    static async downloadPublication(url, filename, signal) {
        return downloadAsFile(url, filename, signal)
    }

    async openPublication(file, options = {}) {
        this.closePublication()
        try {
            const publication = await createSecurePublication(file)
            this.book = publication.book
            this.view = publication.view
            this.view.className = 'reading-house-foliate-view'
            this.host.replaceChildren(this.view)
            this.#bindEvents()
            this.setFlow(options.flow || 'paginated')
            this.setPreferences(options.preferences || {})
            await this.view.init({
                lastLocation: options.locator?.cfi || null,
                showTextStart: true,
            })
            return {
                metadata: this.book.metadata,
                toc: this.book.toc || [],
                sectionCount: this.book.sections?.length || 0,
            }
        } catch (error) {
            this.#emit('error', error)
            throw error
        }
    }

    closePublication() {
        this.#resetSelectionSession('publication_closed')
        this.selectionSession = null
        if (this.view) closeSecurePublication(this.view)
        this.view = null
        this.book = null
        this.decorations.clear()
        this.host.replaceChildren()
    }

    async goTo(target) {
        if (!this.view) return
        this.clearBrowserSelection()
        return this.view.goTo(target)
    }

    async next() {
        if (!this.view) return
        this.clearBrowserSelection()
        return this.view.next()
    }

    async previous() {
        if (!this.view) return
        this.clearBrowserSelection()
        return this.view.prev()
    }

    setFlow(flow) {
        const nextFlow = flow === 'scrolled' ? 'scrolled' : 'paginated'
        if (this.view && nextFlow !== this.flow) this.clearBrowserSelection()
        this.flow = nextFlow
        this.view?.renderer?.setAttribute('flow', this.flow)
        this.#applyStyles()
    }

    setPreferences(preferences = {}) {
        const size = Number(preferences.fontSize)
        if (Number.isFinite(size)) {
            this.preferences.fontSize = Math.max(12, Math.min(36, size))
        }
        this.#applyStyles()
    }

    getCurrentLocator(detail = this.view?.lastLocation || {}) {
        const sectionIndex = Number(detail.section?.current ?? detail.section ?? detail.index ?? 0)
        const section = this.book?.sections?.[sectionIndex]
        const progression = Math.max(
            0,
            Math.min(1, Number(detail.fraction ?? detail.progression ?? 0) || 0),
        )
        return {
            engine: 'foliate-js',
            href: section?.id || detail.href || '',
            cfi: detail.cfi || '',
            section_index: Math.max(0, sectionIndex),
            progression,
        }
    }

    getTOC() {
        return this.book?.toc || []
    }

    hrefIdentifiesSection(href, index) {
        const candidate = canonicalHref(href)
        if (!candidate) return false
        const section = this.#sectionHrefs(index)
        return candidate === section.resource || candidate === section.manifest
    }

    hrefsIdentifySameSection(first, second, index) {
        return this.hrefIdentifiesSection(first, index)
            && this.hrefIdentifiesSection(second, index)
    }

    onRelocate(callback) { return this.#subscribe('relocate', callback) }
    onLink(callback) { return this.#subscribe('link', callback) }
    onSelection(callback) { return this.#subscribe('selection', callback) }
    onSelectionState(callback) { return this.#subscribe('selectionState', callback) }
    onAnnotation(callback) { return this.#subscribe('annotation', callback) }
    onSectionLoad(callback) { return this.#subscribe('sectionLoad', callback) }
    onLocatorInvalid(callback) { return this.#subscribe('locatorInvalid', callback) }
    onError(callback) { return this.#subscribe('error', callback) }

    recordKey(record) {
        const type = record?.kind === 'xiaxia' || record?.record_type === 'thought'
            ? 'thought' : 'annotation'
        return `${type}:${String(record?.id || '')}`
    }

    recordsForValue(value) {
        return [...this.decorations.values()].filter(item => item.value === value)
    }

    async addDecoration(record) {
        const cfi = record?.engine_locator?.cfi
        if (!cfi || !record?.id || !this.view) return false
        const recordKey = this.recordKey(record)
        const previous = this.decorations.get(recordKey)
        if (previous?.value && previous.value !== cfi) {
            this.decorations.delete(recordKey)
            await this.view.deleteAnnotation({ value: previous.value })
            if (this.recordsForValue(previous.value).length) await this.#drawValue(previous.value)
        }
        this.decorations.set(recordKey, {
            recordKey,
            recordType: record.kind === 'xiaxia' ? 'thought' : 'annotation',
            id: String(record.id),
            value: cfi,
            kind: record.kind === 'xiaxia' ? 'xiaxia' : 'user',
            expectedText: String(record.selected_text || ''),
            locator: record.engine_locator,
        })
        return this.#drawValue(cfi)
    }

    async removeDecoration(recordType, recordId) {
        const type = recordType === 'xiaxia' || recordType === 'thought' ? 'thought' : 'annotation'
        const old = this.decorations.get(`${type}:${String(recordId)}`)
        if (!old || !this.view) return
        this.decorations.delete(old.recordKey)
        await this.view.deleteAnnotation({ value: old.value })
        if ([...this.decorations.values()].some(item => item.value === old.value)) {
            await this.#drawValue(old.value)
        }
    }

    async locatorFromSeed(seed, { navigate = false } = {}) {
        const expected = this.#expectedSection(seed)
        if (navigate) {
            const resolved = await this.goTo(expected.href)
            if (Number(resolved?.index) !== expected.index) {
                throw this.#locatorError('locator_mapping_missing')
            }
        }
        const content = this.#loadedContent(expected)
        const range = rangeFromCanonicalOffsets(
            content.doc, seed.original_start, seed.original_end)
        const actualText = textFromRange(content.doc, range)
        const expectedText = normalizeTextV1(seed?.text?.highlight)
        if (!expectedText || normalizeTextV1(actualText) !== expectedText) {
            throw this.#locatorError('locator_mapping_validation_failed')
        }
        const cfi = this.view.getCFI(content.index, range)
        const locator = {
            ...seed,
            href: expected.href,
            cfi,
            section_index: content.index,
            progression: this.getCurrentLocator().progression,
            locator_integrity_version: 1,
            browser_truth: {
                href: expected.href,
                section_index: content.index,
                text: actualText,
            },
        }
        await this.verifyLocator(locator, expectedText)
        return locator
    }

    async verifyLocator(locator, expectedText, { navigate = false } = {}) {
        if (!this.view || !locator?.cfi) throw this.#locatorError('locator_invalid_cfi')
        const expected = this.#expectedSection(locator)
        let resolved
        try { resolved = this.view.resolveNavigation?.(locator.cfi) }
        catch { throw this.#locatorError('locator_invalid_cfi') }
        if (!resolved || Number(resolved.index) !== expected.index) {
            throw this.#locatorError('locator_mapping_validation_failed')
        }
        if (navigate) await this.goTo(locator.cfi)
        const content = this.#loadedContent(expected)
        let range
        try {
            range = typeof resolved.anchor === 'function'
                ? resolved.anchor(content.doc) : resolved.anchor
        } catch { throw this.#locatorError('locator_invalid_cfi') }
        if (!range) throw this.#locatorError('locator_invalid_cfi')
        const actualText = textFromRange(content.doc, range)
        if (normalizeTextV1(actualText) !== normalizeTextV1(expectedText)) {
            throw this.#locatorError('locator_mapping_validation_failed')
        }
        return {
            href: expected.href,
            section_index: expected.index,
            text: actualText,
            range,
        }
    }

    clearBrowserSelection() {
        this.#resetSelectionSession('cleared')
        for (const content of this.view?.renderer?.getContents?.() || []) {
            content.doc?.defaultView?.getSelection?.()?.removeAllRanges?.()
        }
        globalThis.getSelection?.()?.removeAllRanges?.()
    }

    getSelectionDiagnostics() {
        const session = this.selectionSession
        if (!session) return { state: 'idle', flow: this.flow }
        return {
            state: session.state,
            flow: this.flow,
            href: session.href,
            section_index: session.index,
            pointer_down: session.pointerDown,
            touch_down: session.touchDown,
            page_before: session.pageBefore,
            page_after: session.pageAfter,
            relocation_during_selection: session.relocated,
            selected_text_length: session.selectedTextLength || 0,
        }
    }

    async #drawValue(value) {
        const records = this.recordsForValue(value)
        if (!records.length) return false
        const valid = []
        for (const record of records) {
            try {
                await this.verifyLocator(record.locator, record.expectedText)
                valid.push(record)
            } catch (error) {
                if (error.code !== 'locator_mapping_missing') {
                    this.#emit('locatorInvalid', { record, error })
                }
            }
        }
        if (!valid.length) return false
        const kinds = new Set(valid.map(item => item.kind))
        const kind = kinds.size > 1 ? 'shared' : valid[0]?.kind || 'user'
        await this.view.addAnnotation({
            value,
            kind,
            recordKeys: valid.map(item => item.recordKey),
            color: kind === 'xiaxia' ? '#6f8795' : kind === 'shared' ? '#776b7b' : '#8b5c45',
        })
        return true
    }

    #expectedSection(locator) {
        if (!this.view || !locator?.href) throw this.#locatorError('locator_mapping_missing')
        const index = Number(locator.spine_index ?? locator.section_index)
        if (!Number.isInteger(index) || index < 0 || index >= (this.book?.sections?.length || 0)) {
            throw this.#locatorError('locator_mapping_missing')
        }
        const actualHref = this.#sectionHrefs(index).resource
        if (!actualHref || !this.hrefIdentifiesSection(locator.href, index)) {
            throw this.#locatorError('locator_mapping_missing')
        }
        return { index, href: actualHref }
    }

    #sectionHrefs(index) {
        const resource = canonicalHref(this.book?.sections?.[index]?.id || '')
        const idref = this.book?.resources?.spine?.[index]?.idref
        const items = this.book?.resources?.opf
            ?.getElementsByTagNameNS?.('*', 'item') || []
        const item = [...items].find(element => element.getAttribute('id') === idref)
        return {
            resource,
            manifest: canonicalHref(item?.getAttribute('href') || ''),
        }
    }

    #loadedContent(expected) {
        const content = (this.view.renderer?.getContents?.() || [])
            .find(item => Number(item.index) === expected.index)
        if (!content?.doc) throw this.#locatorError('locator_mapping_missing')
        const sectionHref = canonicalHref(this.book.sections[content.index]?.id)
        if (sectionHref !== expected.href) throw this.#locatorError('locator_mapping_missing')
        return content
    }

    #locatorError(code) {
        const error = new Error(code)
        error.code = code
        return error
    }

    #subscribe(name, callback) {
        this.listeners[name].add(callback)
        return () => this.listeners[name].delete(callback)
    }

    #emit(name, detail) {
        for (const callback of this.listeners[name]) callback(detail)
    }

    #applyStyles() {
        if (!this.view) return
        this.view.renderer?.setAttribute('flow', this.flow)
        this.view.renderer?.setStyles?.(`
            html { color-scheme: light; }
            body {
                margin: 0 auto;
                max-width: 42rem;
                padding: 1.25rem clamp(1rem, 5vw, 3rem) 4rem;
                box-sizing: border-box;
                font-size: ${this.preferences.fontSize}px !important;
                line-height: 1.9 !important;
                color: #3f3933;
                background: #f3ecdf;
                font-family: Georgia, "Noto Serif SC", serif;
            }
            img, svg, table { max-width: 100% !important; height: auto; }
            pre { white-space: pre-wrap !important; }
            a { color: #77513f; text-underline-offset: 3px; }
        `)
    }

    #bindEvents() {
        this.view.addEventListener('draw-annotation', event => {
            const { annotation, draw } = event.detail
            const drawFunction = annotation.kind === 'xiaxia'
                ? Overlayer.underline
                : annotation.kind === 'shared' ? Overlayer.outline : Overlayer.highlight
            draw(drawFunction, {
                color: annotation.color,
                width: annotation.kind === 'xiaxia' ? 2 : 3,
                radius: 2,
            })
        })
        this.view.addEventListener('show-annotation', event => {
            const records = this.recordsForValue(event.detail.value)
            this.#emit('annotation', { ...event.detail, records })
        })
        this.view.addEventListener('create-overlay', () => {
            const values = new Set([...this.decorations.values()].map(item => item.value))
            values.forEach(value => this.#drawValue(value))
        })
        this.view.addEventListener('relocate', event => {
            const locator = this.getCurrentLocator(event.detail || {})
            const session = this.selectionSession
            if (this.flow === 'paginated' && session?.active) {
                session.relocated = true
                session.pageAfter = locator
                this.#emit('selectionState', {
                    ...this.getSelectionDiagnostics(),
                    state: 'page_moved',
                    error: 'selection_page_moved',
                })
                return
            }
            this.#emit('relocate', locator)
        })
        this.view.addEventListener('load', event => {
            const { doc, index } = event.detail
            this.#bindSelectionLifecycle(doc, index)
            this.#emit('sectionLoad', {
                href: canonicalHref(this.book.sections[index]?.id || ''), index,
            })
        })
        attachSafeLinkPolicy(this.view, (kind, detail) => {
            this.#emit('link', { kind, ...detail })
        })
    }

    #bindSelectionLifecycle(doc, index) {
        this.#resetSelectionSession('section_changed')
        const session = {
            doc,
            index,
            href: canonicalHref(this.book.sections[index]?.id || ''),
            state: 'idle',
            active: false,
            pointerDown: false,
            touchDown: false,
            relocated: false,
            pageBefore: null,
            pageAfter: null,
            timer: null,
            epoch: 0,
            stableSignature: null,
            selectedTextLength: 0,
        }
        this.selectionSession = session

        const markPointer = value => {
            if (this.selectionSession !== session) return
            session.pointerDown = value
            if (!value && this.#selectionRange(doc)) this.#scheduleSelectionCapture(session, 'pointerup')
        }
        const markTouch = (event, value) => {
            if (this.selectionSession !== session) return
            const hasRange = Boolean(this.#selectionRange(doc))
            session.touchDown = value
            if (hasRange) {
                this.#markSelectionActive(session, 'touch')
                if (this.flow === 'paginated') event.stopImmediatePropagation()
                if (!value) this.#scheduleSelectionCapture(session, 'touchend')
            }
        }
        const blockPaginatorTouchMove = event => {
            if (this.selectionSession !== session || !this.#selectionRange(doc)) return
            this.#markSelectionActive(session, 'touchmove')
            // Do not preventDefault(): Android must keep control of its native
            // selection handles. Only stop foliate paginator's swipe handler.
            if (this.flow === 'paginated') event.stopImmediatePropagation()
        }
        const selectionChanged = event => {
            if (this.selectionSession !== session) return
            const range = this.#selectionRange(doc)
            if (!range) {
                this.#resetSelectionSession('selection_cleared', session)
                return
            }
            this.#markSelectionActive(session, 'selectionchange')
            // Pinned foliate paginator otherwise calls prev()/next() after a
            // 700ms selection debounce when a handle crosses its visible range.
            if (this.flow === 'paginated') event.stopImmediatePropagation()
            this.#scheduleSelectionCapture(session, 'selectionchange')
        }

        doc.addEventListener('selectionchange', selectionChanged, { capture: true })
        doc.addEventListener('touchmove', blockPaginatorTouchMove, { capture: true, passive: true })
        doc.addEventListener('touchend', event => markTouch(event, false), { capture: true, passive: true })
        doc.addEventListener('touchcancel', event => markTouch(event, false), { capture: true, passive: true })
        doc.addEventListener('touchstart', event => markTouch(event, true), { capture: true, passive: true })
        doc.addEventListener('pointerdown', () => markPointer(true))
        doc.addEventListener('pointerup', () => markPointer(false))
        doc.addEventListener('pointercancel', () => markPointer(false))
        doc.addEventListener('keyup', () => {
            if (this.#selectionRange(doc)) this.#scheduleSelectionCapture(session, 'keyup')
        })
    }

    #selectionRange(doc) {
        const selection = doc?.defaultView?.getSelection?.()
        if (!selection || selection.isCollapsed || selection.type !== 'Range' || selection.rangeCount !== 1) {
            return null
        }
        const range = selection.getRangeAt(0)
        const startDocument = range.startContainer?.ownerDocument ||
            (range.startContainer === doc ? doc : null)
        const endDocument = range.endContainer?.ownerDocument ||
            (range.endContainer === doc ? doc : null)
        if (range.collapsed || startDocument !== doc || endDocument !== doc) return null
        return range
    }

    #markSelectionActive(session, reason) {
        if (this.selectionSession !== session) return
        if (!session.active) {
            session.pageBefore = this.getCurrentLocator()
            session.pageAfter = session.pageBefore
            session.relocated = false
        }
        session.active = true
        session.state = 'selecting'
        this.#emit('selectionState', {
            ...this.getSelectionDiagnostics(), state: 'selecting', reason,
        })
    }

    #scheduleSelectionCapture(session, reason) {
        if (this.selectionSession !== session) return
        clearTimeout(session.timer)
        const epoch = ++session.epoch
        session.stableSignature = null
        session.timer = setTimeout(
            () => this.#confirmStableSelection(session, epoch, reason), 180,
        )
    }

    #confirmStableSelection(session, epoch, reason) {
        if (this.selectionSession !== session || epoch !== session.epoch) return
        if (session.pointerDown || session.touchDown) {
            session.timer = setTimeout(
                () => this.#confirmStableSelection(session, epoch, reason), 90,
            )
            return
        }
        const range = this.#selectionRange(session.doc)
        if (!range) {
            this.#resetSelectionSession('selection_cleared', session)
            return
        }
        const signature = this.#rangeSignature(range)
        if (session.stableSignature !== signature) {
            session.stableSignature = signature
            session.timer = setTimeout(
                () => this.#confirmStableSelection(session, epoch, reason), 90,
            )
            return
        }
        this.#finalizeSelection(session, range.cloneRange(), reason)
    }

    #finalizeSelection(session, range, reason) {
        if (this.selectionSession !== session) return
        if (session.relocated) {
            const restore = session.pageBefore?.cfi
            this.#emit('selectionState', {
                ...this.getSelectionDiagnostics(),
                state: 'rejected',
                error: 'selection_page_moved',
                reason,
            })
            this.clearBrowserSelection()
            if (restore) this.view?.goTo(restore).catch(() => {})
            return
        }
        let text
        let cfi
        try {
            text = selectionContext(session.doc, range)
            cfi = this.view.getCFI(session.index, range)
        } catch {
            this.#rejectSelection(session, 'selection_range_invalid', reason)
            return
        }
        const quote = text.highlight
        if (!quote || quote.length > 20_000) {
            this.#rejectSelection(
                session, quote ? 'selection_too_long' : 'selection_range_invalid', reason,
            )
            return
        }
        const actualHref = canonicalHref(this.book.sections[session.index]?.id || '')
        if (!actualHref || actualHref !== session.href) {
            this.#rejectSelection(session, 'selection_section_mismatch', reason)
            return
        }
        session.state = 'stable'
        session.selectedTextLength = quote.length
        session.pageAfter = this.getCurrentLocator()
        const detail = {
            quote,
            text,
            cfi,
            href: actualHref,
            section_index: session.index,
            start_section_index: session.index,
            end_section_index: session.index,
            progression: session.pageAfter.progression,
            locator_integrity_version: 1,
            browser_truth: {
                href: actualHref,
                section_index: session.index,
                text: quote,
            },
            selection_state: 'stable',
            page_before: session.pageBefore,
            page_after: session.pageAfter,
        }
        this.#emit('selectionState', {
            ...this.getSelectionDiagnostics(), state: 'stable', reason,
        })
        this.#emit('selection', detail)
    }

    #rejectSelection(session, error, reason) {
        this.#emit('selectionState', {
            ...this.getSelectionDiagnostics(), state: 'rejected', error, reason,
        })
        this.clearBrowserSelection()
    }

    #resetSelectionSession(reason, expected = this.selectionSession) {
        const session = this.selectionSession
        if (!session || session !== expected) return
        clearTimeout(session.timer)
        const shouldEmit = session.active || session.state !== 'idle'
        session.active = false
        session.state = 'idle'
        session.pointerDown = false
        session.touchDown = false
        session.relocated = false
        session.timer = null
        session.stableSignature = null
        session.selectedTextLength = 0
        if (shouldEmit) this.#emit('selectionState', {
            ...this.getSelectionDiagnostics(), state: 'cleared', reason,
        })
    }

    #rangeSignature(range) {
        const path = node => {
            const parts = []
            let current = node
            while (current?.parentNode && parts.length < 16) {
                parts.push(Array.prototype.indexOf.call(current.parentNode.childNodes, current))
                current = current.parentNode
            }
            return parts.reverse().join('.')
        }
        return [
            path(range.startContainer), range.startOffset,
            path(range.endContainer), range.endOffset,
            normalizeTextV1(range.toString()),
        ].join('|')
    }
}
