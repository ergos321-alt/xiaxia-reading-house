import {
    makeBook,
} from '../vendor/foliate-js/view.js'
import { Overlayer } from '../vendor/foliate-js/overlayer.js'

const MARKUP_TYPES = new Set([
    'application/xhtml+xml',
    'text/html',
    'image/svg+xml',
])

const sanitizePublicationMarkup = (value, mediaType) => {
    const source = String(value ?? '')
    const parser = new DOMParser()
    let document = parser.parseFromString(source, mediaType)
    if (document.querySelector('parsererror')) {
        document = parser.parseFromString(source, 'text/html')
    }
    document.querySelectorAll(
        'script, iframe, frame, frameset, object, embed, applet, base, '
        + 'meta[http-equiv="refresh"]',
    ).forEach(element => element.remove())
    document.querySelectorAll('*').forEach(element => {
        for (const attribute of [...element.attributes]) {
            const name = attribute.name.toLowerCase()
            const value = attribute.value.trim().toLowerCase()
            if (name.startsWith('on') || name === 'srcdoc') {
                element.removeAttribute(attribute.name)
            } else if (
                ['href', 'src', 'action', 'formaction', 'xlink:href'].includes(name)
                && value.startsWith('javascript:')
            ) {
                element.removeAttribute(attribute.name)
            }
        }
    })
    return new XMLSerializer().serializeToString(document)
}

const secureBook = book => {
    const target = book.transformTarget
    target?.addEventListener('load', event => {
        if (event.detail?.isScript) event.detail.allow = false
    })
    target?.addEventListener('data', event => {
        const { detail } = event
        if (!MARKUP_TYPES.has(detail?.type)) return
        detail.data = Promise.resolve(detail.data).then(value =>
            sanitizePublicationMarkup(value, detail.type))
    })
    return book
}

export const probeRangeSupport = async (url, signal) => {
    const started = performance.now()
    const response = await fetch(url, {
        headers: { Range: 'bytes=0-0' },
        cache: 'no-store',
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        signal,
    })
    const headers = {
        status: response.status,
        acceptRanges: response.headers.get('accept-ranges'),
        contentRange: response.headers.get('content-range'),
        contentLength: response.headers.get('content-length'),
        durationMs: Math.round(performance.now() - started),
    }
    await response.body?.cancel()
    return {
        supported: response.status === 206 && Boolean(headers.contentRange),
        ...headers,
    }
}

export const downloadAsFile = async (url, filename, signal) => {
    const started = performance.now()
    const response = await fetch(url, {
        cache: 'no-store',
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        signal,
    })
    if (!response.ok) throw new Error(`source_download_${response.status}`)
    const blob = await response.blob()
    return {
        file: new File([blob], filename || 'publication.epub', {
            type: blob.type || 'application/epub+zip',
        }),
        downloadMs: Math.round(performance.now() - started),
        contentLength: response.headers.get('content-length'),
    }
}

export const createSecurePublication = async file => {
    const book = secureBook(await makeBook(file))
    const view = document.createElement('foliate-view')
    await view.open(book)
    return { book, view }
}

export const attachSafeLinkPolicy = (view, logger) => {
    view.addEventListener('external-link', event => {
        event.preventDefault()
        logger('external-link-blocked', { href: event.detail?.href_ })
    })
    view.addEventListener('link', event => {
        const anchor = event.detail?.a
        const epubType = anchor?.getAttributeNS?.(
            'http://www.idpf.org/2007/ops', 'type')
            ?? anchor?.getAttribute?.('epub:type')
            ?? ''
        const role = anchor?.getAttribute?.('role') ?? ''
        const footnote = /(?:^|\s)(?:noteref|doc-noteref)(?:\s|$)/
            .test(`${epubType} ${role}`)
        logger(footnote ? 'footnote-link' : 'internal-link', {
            href: event.detail?.href,
            epubType,
            role,
        })
    })
}

export const drawMemoryHighlight = event => {
    const color = event.detail.annotation?.color ?? '#8b5c45'
    event.detail.draw(Overlayer.highlight, { color })
}

export const readApproximateMemory = () => {
    const memory = performance.memory
    if (!memory) return { supported: false }
    return {
        supported: true,
        usedJSHeapBytes: memory.usedJSHeapSize,
        totalJSHeapBytes: memory.totalJSHeapSize,
        jsHeapLimitBytes: memory.jsHeapSizeLimit,
    }
}

export const closePublication = view => {
    if (!view) return
    const book = view.book
    view.close()
    book?.destroy?.()
    view.remove()
}

export { sanitizePublicationMarkup }
