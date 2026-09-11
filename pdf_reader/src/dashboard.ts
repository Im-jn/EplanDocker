import './dashboard.css'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.mjs?url'

function installPdfJsCollectionPolyfills(): void {
  const mapPrototype = Map.prototype as Map<unknown, unknown> & {
    getOrInsertComputed?: (key: unknown, callbackfn: (key: unknown) => unknown) => unknown
    getOrInsert?: (key: unknown, value: unknown) => unknown
  }
  if (!mapPrototype.getOrInsertComputed) {
    mapPrototype.getOrInsertComputed = function (key, callbackfn) {
      if (!this.has(key)) this.set(key, callbackfn(key))
      return this.get(key)
    }
  }
  if (!mapPrototype.getOrInsert) {
    mapPrototype.getOrInsert = function (key, value) {
      if (!this.has(key)) this.set(key, value)
      return this.get(key)
    }
  }
}

installPdfJsCollectionPolyfills()
GlobalWorkerOptions.workerSrc = workerUrl

type Job = {
  id: string
  document_id: string
  original_filename: string
  pages: number[]
  status: string
  progress: number
  stage: string
  message: string
  error?: string | null
  reader_status: string
  reader_progress: number
  reader_message: string
  reader_error?: string | null
  result_available: boolean
  document_ready: boolean
  result_url: string
  source_available: boolean
  viewer_url?: string
  created_at: string
}

type CachedPdf = {
  cache_id: string
  document_id?: string | null
  filename: string
  relative_path: string
  size: number
  page_count?: number | null
  job_id?: string | null
  status: string
}

const app = document.querySelector<HTMLDivElement>('#app')
if (!app) throw new Error('Missing #app')

app.innerHTML = `
  <div class="dashboard-shell">
    <header class="dashboard-header">
      <div><p class="dashboard-eyebrow">Eplan processing service</p><h1>Task dashboard</h1></div>
      <nav aria-label="Primary navigation">
        <a class="dashboard-nav-link dashboard-reader-link" id="reader-link" href="/viewer">Open PDF reader →</a>
      </nav>
    </header>
    <main class="dashboard-main">
      <section class="upload-panel" aria-labelledby="upload-title">
        <div class="section-heading">
          <div><p class="dashboard-eyebrow">New batch</p><h2 id="upload-title">Submit PDFs</h2></div>
          <button id="submit-batch" class="dashboard-button primary" type="button" disabled>Submit batch</button>
        </div>
        <label class="file-drop" id="file-drop" for="pdf-files">
          <span>Drop PDF files here</span>
          <small>or click to browse. Every page is selected by default.</small>
          <input id="pdf-files" type="file" accept="application/pdf,.pdf" multiple />
        </label>
        <div id="selected-files" class="selected-files" aria-live="polite"></div>
        <p id="upload-status" class="dashboard-status" aria-live="polite"></p>
      </section>
      <section class="dashboard-section" aria-labelledby="jobs-title">
        <div class="section-heading">
          <div><p class="dashboard-eyebrow">Queue</p><h2 id="jobs-title">Parsing tasks</h2></div>
          <button id="refresh-jobs" class="dashboard-button" type="button">Refresh</button>
        </div>
        <div id="jobs" class="task-list" aria-live="polite"></div>
      </section>
      <section class="dashboard-section" aria-labelledby="cache-title">
        <div class="section-heading"><div><p class="dashboard-eyebrow">Storage</p><h2 id="cache-title">Cached PDFs</h2></div></div>
        <div id="cached-pdfs" class="cache-list" aria-live="polite"></div>
      </section>
      <section class="dashboard-section" aria-labelledby="documents-title">
        <div class="section-heading"><div><p class="dashboard-eyebrow">Document library</p><h2 id="documents-title">Parsed documents</h2></div></div>
        <div id="documents" class="document-grid" aria-live="polite"></div>
      </section>
    </main>
    <dialog id="action-confirm-dialog" class="action-confirm-dialog" aria-labelledby="action-confirm-title">
      <div class="action-confirm-card">
        <div class="action-confirm-icon" aria-hidden="true">!</div>
        <div>
          <p class="dashboard-eyebrow">Confirm action</p>
          <h2 id="action-confirm-title">Confirm</h2>
        </div>
        <p id="action-confirm-message" class="action-confirm-message"></p>
        <div class="action-confirm-actions">
          <button id="action-confirm-cancel" class="dashboard-button" type="button">Cancel</button>
          <button id="action-confirm-accept" class="dashboard-button danger" type="button">Delete</button>
        </div>
      </div>
    </dialog>
  </div>
`

const filesInput = document.querySelector<HTMLInputElement>('#pdf-files')!
const fileDrop = document.querySelector<HTMLLabelElement>('#file-drop')!
const selectedFiles = document.querySelector<HTMLDivElement>('#selected-files')!
const submitButton = document.querySelector<HTMLButtonElement>('#submit-batch')!
const uploadStatus = document.querySelector<HTMLParagraphElement>('#upload-status')!
const jobsRoot = document.querySelector<HTMLDivElement>('#jobs')!
const cachedPdfsRoot = document.querySelector<HTMLDivElement>('#cached-pdfs')!
const documentsRoot = document.querySelector<HTMLDivElement>('#documents')!
const refreshButton = document.querySelector<HTMLButtonElement>('#refresh-jobs')!
const actionConfirmDialog = document.querySelector<HTMLDialogElement>('#action-confirm-dialog')!
const actionConfirmTitle = document.querySelector<HTMLHeadingElement>('#action-confirm-title')!
const actionConfirmMessage = document.querySelector<HTMLParagraphElement>('#action-confirm-message')!
const actionConfirmCancel = document.querySelector<HTMLButtonElement>('#action-confirm-cancel')!
const actionConfirmAccept = document.querySelector<HTMLButtonElement>('#action-confirm-accept')!
type PdfSelection = {
  file: File
  pageCount: number | null
  selectedPages: Set<number>
  error: string | null
}

let selections: PdfSelection[] = []
let pageDrag: { fileIndex: number; select: boolean } | null = null
let confirmResolver: ((confirmed: boolean) => void) | null = null

function closeActionConfirm(confirmed: boolean): void {
  if (actionConfirmDialog.open) actionConfirmDialog.close()
  const resolve = confirmResolver
  confirmResolver = null
  resolve?.(confirmed)
}

function confirmAction(title: string, message: string, acceptLabel: string): Promise<boolean> {
  if (confirmResolver) closeActionConfirm(false)
  actionConfirmTitle.textContent = title
  actionConfirmMessage.textContent = message
  actionConfirmAccept.textContent = acceptLabel
  actionConfirmAccept.classList.toggle('danger', acceptLabel === 'Delete')
  actionConfirmDialog.showModal()
  actionConfirmCancel.focus()
  return new Promise((resolve) => {
    confirmResolver = resolve
  })
}

actionConfirmCancel.addEventListener('click', () => closeActionConfirm(false))
actionConfirmAccept.addEventListener('click', () => closeActionConfirm(true))
actionConfirmDialog.addEventListener('cancel', (event) => {
  event.preventDefault()
  closeActionConfirm(false)
})
actionConfirmDialog.addEventListener('click', (event) => {
  if (event.target === actionConfirmDialog) closeActionConfirm(false)
})

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character] ?? character)
}

function selectionSummary(selection: PdfSelection): string {
  if (selection.error) return 'PDF could not be read'
  if (selection.pageCount === null) return 'Reading page count…'
  const selected = selection.selectedPages.size
  if (selected === selection.pageCount) return `All ${selection.pageCount} pages selected`
  return `${selected} of ${selection.pageCount} pages selected`
}

function renderSelectedFiles(): void {
  submitButton.disabled = selections.length === 0 || selections.some(
    (selection) => selection.pageCount === null || Boolean(selection.error) || selection.selectedPages.size === 0,
  )
  if (!selections.length) {
    selectedFiles.innerHTML = '<p class="empty-state">No PDFs selected.</p>'
    return
  }
  selectedFiles.innerHTML = selections.map((selection, index) => `
    <div class="selected-file">
      <div class="selected-file-meta">
        <strong>${escapeHtml(selection.file.name)}</strong>
        <small>${(selection.file.size / 1024 / 1024).toFixed(1)} MB</small>
      </div>
      ${selection.pageCount === null || selection.error ? `
        <div class="page-picker-state${selection.error ? ' is-error' : ''}">${escapeHtml(selectionSummary(selection))}</div>
      ` : `
        <details class="page-picker" data-file-index="${index}">
          <summary><span>Target pages</span><strong class="page-picker-summary">${selectionSummary(selection)}</strong></summary>
          <div class="page-picker-popover">
            <div class="page-picker-actions">
              <button class="page-picker-action" data-action="all" type="button">Select all</button>
              <button class="page-picker-action" data-action="none" type="button">Clear all</button>
              <span>Drag across pages to select or clear</span>
            </div>
            <div class="page-options" role="group" aria-label="Target pages for ${escapeHtml(selection.file.name)}">
              ${Array.from({ length: selection.pageCount }, (_, pageIndex) => {
                const page = pageIndex + 1
                const checked = selection.selectedPages.has(page)
                return `<button class="page-option${checked ? ' is-selected' : ''}" type="button" data-page="${page}" aria-pressed="${checked}"><i aria-hidden="true"></i><span>${page}</span></button>`
              }).join('')}
            </div>
          </div>
        </details>
      `}
    </div>
  `).join('')
}

function setPageSelected(fileIndex: number, page: number, selected: boolean): void {
  const selection = selections[fileIndex]
  if (!selection?.pageCount || page < 1 || page > selection.pageCount) return
  if (selected) selection.selectedPages.add(page)
  else selection.selectedPages.delete(page)
  const picker = selectedFiles.querySelector<HTMLDetailsElement>(`.page-picker[data-file-index="${fileIndex}"]`)
  const option = picker?.querySelector<HTMLButtonElement>(`.page-option[data-page="${page}"]`)
  option?.classList.toggle('is-selected', selected)
  option?.setAttribute('aria-pressed', String(selected))
  const summary = picker?.querySelector<HTMLElement>('.page-picker-summary')
  if (summary) summary.textContent = selectionSummary(selection)
  submitButton.disabled = selections.some(
    (item) => item.pageCount === null || Boolean(item.error) || item.selectedPages.size === 0,
  )
}

async function inspectPdf(selection: PdfSelection): Promise<void> {
  try {
    const data = new Uint8Array(await selection.file.arrayBuffer())
    const task = getDocument({ data })
    const pdf = await task.promise
    selection.pageCount = pdf.numPages
    selection.selectedPages = new Set(Array.from({ length: pdf.numPages }, (_, index) => index + 1))
    await pdf.destroy()
  } catch (error) {
    selection.error = error instanceof Error ? error.message : String(error)
  }
}

async function addFiles(incoming: FileList | File[]): Promise<void> {
  const candidates = Array.from(incoming).filter(
    (file) => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'),
  )
  const known = new Set(selections.map(({ file }) => `${file.name}:${file.size}:${file.lastModified}`))
  const additions = candidates
    .filter((file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`))
    .map((file): PdfSelection => ({ file, pageCount: null, selectedPages: new Set(), error: null }))
  selections.push(...additions)
  uploadStatus.textContent = candidates.length === Array.from(incoming).length
    ? ''
    : 'Non-PDF files were ignored.'
  renderSelectedFiles()
  await Promise.all(additions.map(inspectPdf))
  renderSelectedFiles()
}

function statusLabel(job: Job): string {
  if (job.status === 'pause_requested') return 'Pausing'
  if (job.status === 'paused') return 'Paused'
  if (job.status === 'delete_requested') return 'Deleting'
  if (job.status === 'succeeded' && job.reader_status === 'pending') return 'Ready to prepare reader'
  if (job.status === 'succeeded' && job.reader_status === 'building') return 'Publishing reader data'
  if (job.status === 'succeeded' && job.reader_status === 'failed') return 'Reader publication failed'
  if (job.document_ready) return 'Ready'
  return job.status.replaceAll('_', ' ')
}

function renderJobs(jobs: Job[]): void {
  if (!jobs.length) {
    jobsRoot.innerHTML = '<p class="empty-state">No parsing tasks have been submitted.</p>'
    return
  }
  jobsRoot.innerHTML = jobs.map((job) => {
    const publishing = job.status === 'succeeded' && !job.document_ready
    const progress = publishing ? job.reader_progress : job.progress
    const message = publishing
      ? (job.reader_error || job.reader_message || 'Open the reader to prepare this document')
      : (job.error || job.message || '')
    const canPause = job.status === 'queued' || job.status === 'running'
    const canResume = job.status === 'paused' || job.status === 'failed'
    return `
    <article class="task-row">
      <div class="task-primary"><strong>${escapeHtml(job.original_filename)}</strong><span>${escapeHtml(statusLabel(job))} · ${escapeHtml(job.stage)}</span></div>
      <div class="task-progress"><div class="progress-track" role="progressbar" aria-label="${escapeHtml(job.original_filename)} progress" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"><div style="width:${progress}%"></div></div><span>${progress}%</span></div>
      <p>${escapeHtml(message)}</p>
      <div class="task-actions">
        ${canPause ? `<button type="button" data-job-action="pause" data-job-id="${job.id}">Pause</button>` : ''}
        ${canResume ? `<button type="button" data-job-action="resume" data-job-id="${job.id}">${job.status === 'failed' ? 'Resume' : 'Continue'}</button>` : ''}
        ${job.result_available ? `<a href="${job.result_url}/download">Result JSON</a>` : ''}
        ${job.viewer_url ? `<a href="${job.viewer_url}">Open reader</a>` : ''}
        ${job.status === 'delete_requested'
          ? '<span class="action-pending">Deletion pending…</span>'
          : `<button class="is-danger" type="button" data-job-action="delete" data-job-id="${job.id}" data-filename="${escapeHtml(job.original_filename)}">Delete</button>`}
      </div>
    </article>
  `}).join('')
}

function renderCachedPdfs(pdfs: CachedPdf[]): void {
  if (!pdfs.length) {
    cachedPdfsRoot.innerHTML = '<p class="empty-state">No PDFs are currently stored in storage.</p>'
    return
  }
  cachedPdfsRoot.innerHTML = pdfs.map((pdf) => `
    <article class="cache-row">
      <span class="document-icon" aria-hidden="true">PDF</span>
      <div class="cache-primary">
        <strong>${escapeHtml(pdf.filename)}</strong>
        <small>${pdf.page_count ? `${pdf.page_count} pages · ` : ''}${(pdf.size / 1024 / 1024).toFixed(1)} MB · ${escapeHtml(pdf.status)}</small>
        <code>${escapeHtml(pdf.relative_path)}</code>
      </div>
      <div class="cache-actions">
        <button type="button" data-cache-action="reprocess" data-cache-id="${pdf.cache_id}" data-filename="${escapeHtml(pdf.filename)}">Reprocess</button>
        ${pdf.status === 'delete_requested'
          ? '<span class="action-pending">Deletion pending…</span>'
          : `<button class="is-danger" type="button" data-cache-action="delete" data-cache-id="${pdf.cache_id}" data-filename="${escapeHtml(pdf.filename)}">Delete</button>`}
      </div>
    </article>
  `).join('')
}

function renderDocuments(documents: Job[]): void {
  const readerLink = document.querySelector<HTMLAnchorElement>('#reader-link')!
  if (!documents.length) {
    readerLink.hidden = true
    documentsRoot.innerHTML = '<p class="empty-state">Parsed documents will appear here.</p>'
    return
  }
  const readableDocument = documents.find((document) => document.source_available)
  readerLink.hidden = !readableDocument
  if (readableDocument) readerLink.href = `/viewer/${readableDocument.document_id}`
  documentsRoot.innerHTML = documents.map((document) => `
    <div class="document-card">
      <span class="document-icon" aria-hidden="true">PDF</span>
      <strong>${escapeHtml(document.original_filename)}</strong>
      <small>${document.source_available
        ? (document.document_ready ? 'Reader ready' : escapeHtml(document.reader_message || 'Open to prepare reader data'))
        : 'Source PDF deleted · parsed result retained'}</small>
      <div class="document-progress" role="progressbar" aria-valuenow="${document.reader_progress}" aria-valuemin="0" aria-valuemax="100"><div style="width:${document.reader_progress}%"></div></div>
      <span class="document-links"><a href="${document.result_url}/download">Result JSON</a>${document.source_available ? ` <a href="/viewer/${document.document_id}">Open reader</a>` : ''}</span>
    </div>
  `).join('')
}

async function refresh(): Promise<void> {
  try {
    const [jobsResponse, documentsResponse, cachedResponse] = await Promise.all([
      fetch('/api/v1/parsing-jobs?limit=100'), fetch('/api/v1/documents'), fetch('/api/v1/cached-pdfs'),
    ])
    if (!jobsResponse.ok || !documentsResponse.ok || !cachedResponse.ok) throw new Error('API request failed')
    renderJobs(((await jobsResponse.json()) as { jobs: Job[] }).jobs)
    renderDocuments(((await documentsResponse.json()) as { documents: Job[] }).documents)
    renderCachedPdfs(((await cachedResponse.json()) as { pdfs: CachedPdf[] }).pdfs)
  } catch (error) {
    jobsRoot.innerHTML = `<p class="empty-state error">Unable to load tasks: ${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`
    cachedPdfsRoot.innerHTML = '<p class="empty-state error">Unable to load cached PDFs.</p>'
  }
}

filesInput.addEventListener('change', () => {
  void addFiles(filesInput.files ?? [])
  filesInput.value = ''
})

for (const eventName of ['dragenter', 'dragover']) {
  fileDrop.addEventListener(eventName, (event) => {
    event.preventDefault()
    fileDrop.classList.add('is-dragging')
  })
}

for (const eventName of ['dragleave', 'drop']) {
  fileDrop.addEventListener(eventName, (event) => {
    event.preventDefault()
    fileDrop.classList.remove('is-dragging')
  })
}

fileDrop.addEventListener('drop', (event) => {
  if (event.dataTransfer?.files.length) void addFiles(event.dataTransfer.files)
})

selectedFiles.addEventListener('click', (event) => {
  const action = (event.target as HTMLElement).closest<HTMLButtonElement>('.page-picker-action')
  if (!action) return
  const picker = action.closest<HTMLDetailsElement>('.page-picker')
  if (!picker) return
  const fileIndex = Number(picker.dataset.fileIndex)
  const selection = selections[fileIndex]
  if (!selection?.pageCount) return
  const select = action.dataset.action === 'all'
  selection.selectedPages = select
    ? new Set(Array.from({ length: selection.pageCount }, (_, index) => index + 1))
    : new Set()
  picker.querySelectorAll<HTMLButtonElement>('.page-option').forEach((option) => {
    option.classList.toggle('is-selected', select)
    option.setAttribute('aria-pressed', String(select))
  })
  const summary = picker.querySelector<HTMLElement>('.page-picker-summary')
  if (summary) summary.textContent = selectionSummary(selection)
  submitButton.disabled = selections.some(
    (item) => item.pageCount === null || Boolean(item.error) || item.selectedPages.size === 0,
  )
})

selectedFiles.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return
  const option = (event.target as HTMLElement).closest<HTMLButtonElement>('.page-option')
  const picker = option?.closest<HTMLDetailsElement>('.page-picker')
  if (!option || !picker) return
  event.preventDefault()
  const fileIndex = Number(picker.dataset.fileIndex)
  const page = Number(option.dataset.page)
  const select = option.getAttribute('aria-pressed') !== 'true'
  pageDrag = { fileIndex, select }
  setPageSelected(fileIndex, page, select)
})

selectedFiles.addEventListener('pointerover', (event) => {
  if (!pageDrag || (event.buttons & 1) === 0) return
  const option = (event.target as HTMLElement).closest<HTMLButtonElement>('.page-option')
  const picker = option?.closest<HTMLDetailsElement>('.page-picker')
  if (!option || !picker || Number(picker.dataset.fileIndex) !== pageDrag.fileIndex) return
  setPageSelected(pageDrag.fileIndex, Number(option.dataset.page), pageDrag.select)
})

selectedFiles.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return
  const option = (event.target as HTMLElement).closest<HTMLButtonElement>('.page-option')
  const picker = option?.closest<HTMLDetailsElement>('.page-picker')
  if (!option || !picker) return
  event.preventDefault()
  setPageSelected(
    Number(picker.dataset.fileIndex),
    Number(option.dataset.page),
    option.getAttribute('aria-pressed') !== 'true',
  )
})

window.addEventListener('pointerup', () => {
  pageDrag = null
})

async function runAction(url: string, method: 'POST' | 'DELETE'): Promise<void> {
  const response = await fetch(url, { method })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`)
}

jobsRoot.addEventListener('click', async (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-job-action]')
  if (!button) return
  const action = button.dataset.jobAction
  const jobId = button.dataset.jobId
  if (!action || !jobId) return
  if (action === 'delete') {
    const filename = button.dataset.filename || 'this PDF'
    const confirmed = await confirmAction(
      `Delete ${filename}?`,
      'This removes this Queue entry, its parsing result, and its Reader data. The cached PDF is kept and can be processed again later.',
      'Delete',
    )
    if (!confirmed) return
  }
  try {
    button.disabled = true
    const method = action === 'delete' ? 'DELETE' : 'POST'
    const suffix = action === 'delete' ? '' : `/${action}`
    await runAction(`/api/v1/parsing-jobs/${encodeURIComponent(jobId)}${suffix}`, method)
    uploadStatus.textContent = action === 'delete' ? 'Deletion accepted.' : `Task ${action} requested.`
    await refresh()
  } catch (error) {
    uploadStatus.textContent = error instanceof Error ? error.message : String(error)
  } finally {
    button.disabled = false
  }
})

cachedPdfsRoot.addEventListener('click', async (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-cache-action]')
  if (!button) return
  const action = button.dataset.cacheAction
  const cacheId = button.dataset.cacheId
  if (!action || !cacheId) return
  const filename = button.dataset.filename || 'this PDF'
  if (action === 'delete') {
    const confirmed = await confirmAction(
      `Delete ${filename}?`,
      'This removes the cached PDF. Completed Queue entries and parsed results are kept; unfinished tasks for this PDF will also be removed.',
      'Delete',
    )
    if (!confirmed) return
  } else {
    const confirmed = await confirmAction(
      `Reprocess ${filename}?`,
      'Existing parsing results and Reader data will be cleared, then a new parsing task will be queued with all pages selected.',
      'Reprocess',
    )
    if (!confirmed) return
  }
  try {
    button.disabled = true
    await runAction(
      `/api/v1/cached-pdfs/${encodeURIComponent(cacheId)}${action === 'reprocess' ? '/reprocess' : ''}`,
      action === 'reprocess' ? 'POST' : 'DELETE',
    )
    uploadStatus.textContent = action === 'reprocess' ? 'Reprocessing task queued.' : 'Cached PDF deletion accepted.'
    await refresh()
  } catch (error) {
    uploadStatus.textContent = error instanceof Error ? error.message : String(error)
  } finally {
    button.disabled = false
  }
})

submitButton.addEventListener('click', async () => {
  try {
    submitButton.disabled = true
    uploadStatus.textContent = 'Uploading batch…'
    const pageSpecs = selections.map((selection) => ({
      pages: [...selection.selectedPages].sort((left, right) => left - right),
    }))
    const body = new FormData()
    selections.forEach(({ file }) => body.append('files', file))
    body.append('page_specs', JSON.stringify(pageSpecs))
    const response = await fetch('/api/v1/parsing-batches', { method: 'POST', body })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || `Upload failed (${response.status})`)
    uploadStatus.textContent = `Batch ${payload.batch_id} accepted with ${payload.jobs.length} task(s).`
    selections = []
    filesInput.value = ''
    renderSelectedFiles()
    await refresh()
  } catch (error) {
    uploadStatus.textContent = error instanceof Error ? error.message : String(error)
  } finally {
    submitButton.disabled = selections.length === 0 || selections.some(
      (selection) => selection.pageCount === null || Boolean(selection.error) || selection.selectedPages.size === 0,
    )
  }
})

refreshButton.addEventListener('click', () => void refresh())
renderSelectedFiles()
void refresh()
window.setInterval(() => void refresh(), 2500)
