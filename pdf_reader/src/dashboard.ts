import './dashboard.css'

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
  result_available: boolean
  document_ready: boolean
  result_url: string
  viewer_url?: string
  created_at: string
}

const app = document.querySelector<HTMLDivElement>('#app')
if (!app) throw new Error('Missing #app')

app.innerHTML = `
  <div class="dashboard-shell">
    <header class="dashboard-header">
      <div><p class="dashboard-eyebrow">Eplan processing service</p><h1>Task dashboard</h1></div>
      <nav aria-label="Primary navigation">
        <a class="dashboard-nav-link is-active" href="/tasks">Tasks</a>
        <a class="dashboard-nav-link" id="reader-link" href="/viewer">PDF reader</a>
      </nav>
    </header>
    <main class="dashboard-main">
      <section class="upload-panel" aria-labelledby="upload-title">
        <div class="section-heading">
          <div><p class="dashboard-eyebrow">New batch</p><h2 id="upload-title">Submit PDFs</h2></div>
          <button id="submit-batch" class="dashboard-button primary" type="button" disabled>Submit batch</button>
        </div>
        <label class="file-drop" for="pdf-files">
          <span>Select one or more PDF files</span>
          <small>Specify target pages per document, or leave them empty to parse every detected diagram page.</small>
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
      <section class="dashboard-section" aria-labelledby="documents-title">
        <div class="section-heading"><div><p class="dashboard-eyebrow">Ready to review</p><h2 id="documents-title">Completed documents</h2></div></div>
        <div id="documents" class="document-grid" aria-live="polite"></div>
      </section>
    </main>
  </div>
`

const filesInput = document.querySelector<HTMLInputElement>('#pdf-files')!
const selectedFiles = document.querySelector<HTMLDivElement>('#selected-files')!
const submitButton = document.querySelector<HTMLButtonElement>('#submit-batch')!
const uploadStatus = document.querySelector<HTMLParagraphElement>('#upload-status')!
const jobsRoot = document.querySelector<HTMLDivElement>('#jobs')!
const documentsRoot = document.querySelector<HTMLDivElement>('#documents')!
const refreshButton = document.querySelector<HTMLButtonElement>('#refresh-jobs')!
let files: File[] = []

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character] ?? character)
}

function parsePages(value: string): number[] {
  if (!value.trim()) return []
  const pages = value.split(',').map((part) => Number(part.trim()))
  if (pages.some((page) => !Number.isInteger(page) || page < 1)) {
    throw new Error('Pages must be positive integers separated by commas.')
  }
  return [...new Set(pages)].sort((left, right) => left - right)
}

function renderSelectedFiles(): void {
  submitButton.disabled = files.length === 0
  if (!files.length) {
    selectedFiles.innerHTML = '<p class="empty-state">No PDFs selected.</p>'
    return
  }
  selectedFiles.innerHTML = files.map((file, index) => `
    <div class="selected-file">
      <div><strong>${escapeHtml(file.name)}</strong><small>${(file.size / 1024 / 1024).toFixed(1)} MB</small></div>
      <label>Target pages<input class="pages-input" data-file-index="${index}" type="text" inputmode="numeric" placeholder="e.g. 1, 2, 8" /></label>
    </div>
  `).join('')
}

function statusLabel(job: Job): string {
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
  jobsRoot.innerHTML = jobs.map((job) => `
    <article class="task-row">
      <div class="task-primary"><strong>${escapeHtml(job.original_filename)}</strong><span>${escapeHtml(statusLabel(job))} · ${escapeHtml(job.stage)}</span></div>
      <div class="task-progress"><div class="progress-track" role="progressbar" aria-label="${escapeHtml(job.original_filename)} progress" aria-valuenow="${job.progress}" aria-valuemin="0" aria-valuemax="100"><div style="width:${job.progress}%"></div></div><span>${job.progress}%</span></div>
      <p>${escapeHtml(job.error || job.message || '')}</p>
      <div class="task-actions">${job.result_available ? `<a href="${job.result_url}/download">Result JSON</a>` : ''}${job.viewer_url ? `<a href="${job.viewer_url}">Open reader</a>` : ''}</div>
    </article>
  `).join('')
}

function renderDocuments(documents: Job[]): void {
  const readerLink = document.querySelector<HTMLAnchorElement>('#reader-link')!
  if (!documents.length) {
    documentsRoot.innerHTML = '<p class="empty-state">Completed and published documents will appear here.</p>'
    return
  }
  readerLink.href = `/viewer/${documents[0].document_id}`
  documentsRoot.innerHTML = documents.map((document) => `
    <a class="document-card" href="/viewer/${document.document_id}"><span class="document-icon" aria-hidden="true">PDF</span><strong>${escapeHtml(document.original_filename)}</strong><small>Processed ${new Date(document.created_at).toLocaleString()}</small></a>
  `).join('')
}

async function refresh(): Promise<void> {
  try {
    const [jobsResponse, documentsResponse] = await Promise.all([
      fetch('/api/v1/parsing-jobs?limit=100'), fetch('/api/v1/documents'),
    ])
    if (!jobsResponse.ok || !documentsResponse.ok) throw new Error('API request failed')
    renderJobs(((await jobsResponse.json()) as { jobs: Job[] }).jobs)
    renderDocuments(((await documentsResponse.json()) as { documents: Job[] }).documents)
  } catch (error) {
    jobsRoot.innerHTML = `<p class="empty-state error">Unable to load tasks: ${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`
  }
}

filesInput.addEventListener('change', () => {
  files = [...(filesInput.files ?? [])]
  renderSelectedFiles()
})

submitButton.addEventListener('click', async () => {
  try {
    submitButton.disabled = true
    uploadStatus.textContent = 'Uploading batch…'
    const pageSpecs = files.map((_, index) => {
      const input = document.querySelector<HTMLInputElement>(`.pages-input[data-file-index="${index}"]`)
      return { pages: parsePages(input?.value ?? '') }
    })
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    body.append('page_specs', JSON.stringify(pageSpecs))
    const response = await fetch('/api/v1/parsing-batches', { method: 'POST', body })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || `Upload failed (${response.status})`)
    uploadStatus.textContent = `Batch ${payload.batch_id} accepted with ${payload.jobs.length} task(s).`
    files = []
    filesInput.value = ''
    renderSelectedFiles()
    await refresh()
  } catch (error) {
    uploadStatus.textContent = error instanceof Error ? error.message : String(error)
  } finally {
    submitButton.disabled = files.length === 0
  }
})

refreshButton.addEventListener('click', () => void refresh())
renderSelectedFiles()
void refresh()
window.setInterval(() => void refresh(), 2500)
