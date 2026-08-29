const DROP = /[\u00ad\u200b\u200c\u200d\ufeff]/g
const BLOCK = new Set([
    'address', 'article', 'aside', 'blockquote', 'caption', 'dd', 'div', 'dl',
    'dt', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4',
    'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre',
    'section', 'table', 'td', 'th', 'tr', 'ul',
])
const SKIP = new Set(['script', 'style', 'template', 'rt', 'rp'])

export const normalizeTextV1 = value => String(value ?? '')
    .normalize('NFC')
    .replace(DROP, '')
    .replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

export const canonicalHref = value => {
    const raw = String(value ?? '').split('#', 1)[0].replaceAll('\\', '/').trim()
    let decoded = raw
    try { decoded = decodeURIComponent(raw) } catch { /* malformed escapes fail at comparison */ }
    const parts = []
    for (const part of decoded.split('/')) {
        if (!part || part === '.') continue
        if (part === '..') parts.pop()
        else parts.push(part)
    }
    return parts.join('/')
}

const buildIndex = root => {
    let text = ''
    let pendingSpace = false
    const positions = []
    const spans = new Map()

    const appendSpace = () => {
        if (text) pendingSpace = true
    }
    const appendText = node => {
        const start = text.length
        const raw = node.data
        for (let offset = 0; offset < raw.length; offset += 1) {
            const normalized = raw[offset].normalize('NFC').replace(DROP, '')
            for (const char of normalized) {
                if (/\s|\u00a0/.test(char)) {
                    appendSpace()
                    continue
                }
                if (pendingSpace && text) {
                    text += ' '
                    positions.push(null)
                }
                pendingSpace = false
                text += char
                positions.push({ node, start: offset, end: offset + 1 })
            }
        }
        spans.set(node, { start, end: text.length, raw })
    }
    const walk = node => {
        if (node.nodeType === Node.TEXT_NODE) return appendText(node)
        if (node.nodeType !== Node.ELEMENT_NODE) return
        const name = node.localName?.toLowerCase() || ''
        if (SKIP.has(name)) return
        if (name === 'br') return appendSpace()
        for (const child of node.childNodes) walk(child)
        if (BLOCK.has(name)) appendSpace()
    }
    walk(root)
    return { text: normalizeTextV1(text), positions, spans }
}

export const selectionContext = (doc, range, contextLength = 120) => {
    const index = buildIndex(doc.body || doc.documentElement)
    const quoteRoot = doc.createElement('div')
    quoteRoot.append(range.cloneContents())
    const quote = buildIndex(quoteRoot).text
    let start = -1
    if (range.startContainer?.nodeType === Node.TEXT_NODE) {
        start = index.positions.findIndex(position => position
            && position.node === range.startContainer
            && position.start >= range.startOffset)
    }
    if (start < 0 || index.text.slice(start, start + quote.length) !== quote) {
        const matches = []
        let cursor = 0
        while (quote && (cursor = index.text.indexOf(quote, cursor)) >= 0) {
            matches.push(cursor)
            cursor += Math.max(1, quote.length)
        }
        start = matches.length === 1 ? matches[0] : -1
    }
    return {
        highlight: quote,
        before: start >= 0 ? index.text.slice(Math.max(0, start - contextLength), start) : '',
        after: start >= 0 ? index.text.slice(start + quote.length, start + quote.length + contextLength) : '',
    }
}

export const textFromRange = (doc, range) => selectionContext(doc, range).highlight

export const rangeFromCanonicalOffsets = (doc, start, end) => {
    const index = buildIndex(doc.body || doc.documentElement)
    let first = Math.max(0, Number(start) || 0)
    let last = Math.min(index.positions.length - 1, Math.max(first, (Number(end) || first + 1) - 1))
    while (first <= last && !index.positions[first]) first += 1
    while (last >= first && !index.positions[last]) last -= 1
    if (first > last) throw new Error('locator_mapping_missing')
    const begin = index.positions[first]
    const finish = index.positions[last]
    const range = doc.createRange()
    range.setStart(begin.node, begin.start)
    range.setEnd(finish.node, finish.end)
    return range
}
