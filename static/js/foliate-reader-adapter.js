import {
    attachSafeLinkPolicy,
    closePublication as closeSecurePublication,
    createSecurePublication,
    downloadAsFile,
} from './foliate-poc-adapter.js'

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
            error: new Set(),
        }
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
        if (this.view) closeSecurePublication(this.view)
        this.view = null
        this.book = null
        this.host.replaceChildren()
    }

    async goTo(target) {
        if (!this.view) return
        return this.view.goTo(target)
    }

    async next() {
        if (!this.view) return
        return this.view.next()
    }

    async previous() {
        if (!this.view) return
        return this.view.prev()
    }

    setFlow(flow) {
        this.flow = flow === 'scrolled' ? 'scrolled' : 'paginated'
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
        const sectionIndex = Number(detail.section ?? detail.index ?? 0)
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

    onRelocate(callback) { return this.#subscribe('relocate', callback) }
    onLink(callback) { return this.#subscribe('link', callback) }
    onSelection(callback) { return this.#subscribe('selection', callback) }
    onError(callback) { return this.#subscribe('error', callback) }

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
        this.view.addEventListener('relocate', event => {
            this.#emit('relocate', this.getCurrentLocator(event.detail || {}))
        })
        this.view.addEventListener('load', event => {
            const { doc, index } = event.detail
            const capture = () => {
                const selection = doc.defaultView?.getSelection()
                if (!selection || selection.isCollapsed || !selection.rangeCount) return
                const range = selection.getRangeAt(0)
                const quote = selection.toString().trim()
                if (!quote) return
                this.#emit('selection', {
                    quote,
                    cfi: this.view.getCFI(index, range),
                    href: this.book.sections[index]?.id || '',
                    section_index: index,
                })
            }
            doc.addEventListener('pointerup', capture)
            doc.addEventListener('keyup', capture)
        })
        attachSafeLinkPolicy(this.view, (kind, detail) => {
            this.#emit('link', { kind, ...detail })
        })
    }
}
