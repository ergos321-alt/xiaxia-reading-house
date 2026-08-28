const body = document.body
const bookId = body.dataset.bookId
const featureEnabled = body.dataset.readerEngineEnabled === 'true'
const query = new URLSearchParams(location.search)

const loadLegacy = async () => {
    body.dataset.readerEngine = 'legacy'
    await import('./reader-utils.js')
    await import('./reader.js')
}

try {
    const response = await fetch(`/api/books/${bookId}`, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || `book_${response.status}`)
    const book = data.book
    const useFoliate = featureEnabled
        && query.get('engine') !== 'legacy'
        && book.format === 'epub'
        && book.publication_ready === true
        && book.reader_engine === 'foliate'
    if (useFoliate) {
        body.dataset.readerEngine = 'foliate'
        body.dataset.textIndexReady = String(book.text_index_status === 'ready')
        body.classList.add('foliate-reader-page')
        document.querySelector('#foliate-readonly-note').hidden = false
        await import('./foliate-reader.js')
    } else {
        await loadLegacy()
    }
} catch (error) {
    const content = document.querySelector('#chapter-content')
    content.textContent = `无法打开这本书：${error.message || error}`
}

